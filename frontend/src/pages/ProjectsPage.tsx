/**
 * Projects and the project dashboard (design document sections 46, 47).
 *
 * Section 47's dashboard counts entities, output, relationships and generated
 * media, then offers two doors: OPEN STUDIO and GENERATE. That is what this
 * shows, with the linter's findings alongside - because the moment to learn
 * that a field will make ten million model calls is before pressing the second
 * button, not after.
 */

import { type FormEvent, type ReactNode, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  useLint,
  usePlan,
  useProject,
  useProjects,
  useRegisterProject,
  useRuns,
  useSchema,
} from "../api/hooks";
import { PageHead } from "../components/Layout";
import {
  Empty,
  ErrorNotice,
  LintList,
  Panel,
  Spinner,
  StateChip,
  Stat,
  formatBytes,
  formatNumber,
  formatWhen,
} from "../components/ui";
import { useStudio } from "../state/store";

export function ProjectsPage(): ReactNode {
  const projects = useProjects();
  const projectId = useStudio((state) => state.projectId);
  const selectProject = useStudio((state) => state.selectProject);
  const register = useRegisterProject();
  const [path, setPath] = useState("");

  const onRegister = (event: FormEvent) => {
    event.preventDefault();
    if (!path.trim()) return;
    register.mutate(
      { path: path.trim() },
      {
        onSuccess: (project) => {
          selectProject(project.id);
          setPath("");
        },
      },
    );
  };

  return (
    <>
      <PageHead
        title="Projects"
        subtitle="A project is a schema, its revisions and everything generated from it."
      />

      <Panel title="Open a project">
        <form onSubmit={onRegister} className="row" style={{ alignItems: "flex-end" }}>
          <div style={{ flex: 1, minWidth: 260 }}>
            <label htmlFor="project-path">Path to a schema file</label>
            <input
              id="project-path"
              value={path}
              placeholder="templates/corporate-directory.yaml"
              onChange={(event) => setPath(event.target.value)}
            />
          </div>
          <button type="submit" className="button-primary" disabled={register.isPending}>
            {register.isPending ? "Opening…" : "Open"}
          </button>
        </form>
        <p className="faint" style={{ marginBottom: 0, fontSize: "0.78rem" }}>
          The path is resolved on the machine running <code>cacophony serve</code>.
        </p>
        {register.isError && <ErrorNotice error={register.error} />}
      </Panel>

      <div style={{ height: 16 }} />

      {projects.isLoading && <Spinner label="Loading projects" />}
      <ErrorNotice error={projects.error} />

      {projects.data && projects.data.length === 0 && (
        <Empty title="No projects yet">
          <p>
            Open one of the shipped templates above, such as{" "}
            <code>templates/corporate-directory.yaml</code>.
          </p>
        </Empty>
      )}

      {projects.data && projects.data.length > 0 && (
        <Panel title={`${projects.data.length} project${projects.data.length === 1 ? "" : "s"}`}>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Path</th>
                  <th>Updated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {projects.data.map((project) => (
                  <tr key={project.id}>
                    <td>
                      <strong>{project.name}</strong>
                      {project.id === projectId && (
                        <span className="badge badge-derived" style={{ marginLeft: 8 }}>
                          selected
                        </span>
                      )}
                    </td>
                    <td className="faint mono truncate" title={project.path ?? ""}>
                      {project.path ?? "inline"}
                    </td>
                    <td className="faint">{formatWhen(project.updated_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        type="button"
                        className="button-sm"
                        onClick={() => selectProject(project.id)}
                      >
                        Select
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {projectId !== null && <Dashboard projectId={projectId} />}
    </>
  );
}

/** Section 47's dashboard. */
function Dashboard({ projectId }: { projectId: number }): ReactNode {
  const navigate = useNavigate();
  const project = useProject(projectId);
  const schema = useSchema(projectId);
  const plan = usePlan(projectId);
  const lint = useLint(projectId);
  const runs = useRuns({ project_id: projectId, limit: 5 });

  if (schema.isError || plan.isError) {
    return (
      <>
        <div style={{ height: 24 }} />
        <ErrorNotice error={schema.error ?? plan.error} />
      </>
    );
  }

  if (!schema.data || !plan.data) {
    return (
      <>
        <div style={{ height: 24 }} />
        <Spinner label="Compiling the schema" />
      </>
    );
  }

  const entities = Object.keys(schema.data.entities).length;
  const relationships = schema.data.relationships.length;
  const estimate = plan.data.estimate;
  const media = estimate.image_calls + estimate.speech_calls;

  return (
    <>
      <div style={{ height: 24 }} />
      <PageHead
        title={schema.data.name}
        subtitle={
          <>
            seed {plan.data.seed}
            {project.data?.revisions.length ? (
              <> · schema revision {project.data.revisions.at(-1)?.version}</>
            ) : null}
          </>
        }
        actions={
          <>
            <button type="button" onClick={() => navigate("/studio")}>
              Open Studio
            </button>
            <button type="button" className="button-primary" onClick={() => navigate("/generate")}>
              Generate
            </button>
          </>
        }
      />

      <div className="grid grid-4">
        <Stat label="Entities" value={entities} tone="violet" />
        <Stat
          label="Output"
          value={formatNumber(estimate.records)}
          note="records"
          tone="cyan"
        />
        <Stat label="Relationships" value={relationships} />
        <Stat
          label="Generated media"
          value={formatNumber(media)}
          note={media === 0 ? "none declared" : "files"}
          tone="magenta"
        />
      </div>

      <div style={{ height: 16 }} />

      <div className="grid grid-2">
        <Panel title="Estimated workload">
          <p className="faint" style={{ marginTop: 0, fontSize: "0.78rem" }}>
            Order of magnitude only (design document section 69).
          </p>
          <table>
            <tbody>
              <tr>
                <td>Field values</td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatNumber(estimate.fields)}
                </td>
              </tr>
              <tr>
                <td>Language-model calls</td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatNumber(estimate.llm_calls)}
                </td>
              </tr>
              <tr>
                <td>Images</td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatNumber(estimate.image_calls)}
                </td>
              </tr>
              <tr>
                <td>Audio</td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatNumber(estimate.speech_calls)}
                </td>
              </tr>
              <tr>
                <td>Storage</td>
                <td className="nums" style={{ textAlign: "right" }}>
                  {formatBytes(estimate.estimated_bytes)}
                </td>
              </tr>
            </tbody>
          </table>
        </Panel>

        <Panel title="Schema linter">
          {lint.isLoading && <Spinner />}
          {lint.data && <LintList issues={lint.data.issues} />}
        </Panel>
      </div>

      {runs.data && runs.data.length > 0 && (
        <>
          <div style={{ height: 16 }} />
          <Panel title="Recent runs">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>State</th>
                    <th style={{ textAlign: "right" }}>Records</th>
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
                      <td className="nums" style={{ textAlign: "right" }}>
                        {formatNumber(run.records_written)}
                      </td>
                      <td className="faint">{formatWhen(run.started_at ?? run.created_at)}</td>
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
