/**
 * The Plugins page (design document sections 44, 46).
 *
 * Section 46 lists Plugins in the navigation, and it has been a placeholder
 * since the Studio was built. This is it.
 *
 * What it mostly does is explain a decision, because the decision is the
 * feature: **Cacophony does not load Python from a project directory.** A schema
 * arrives by email, in a Git repository, inside a bundle — and if opening one
 * could load its own code, every other safety property in the platform would be
 * decoration. So a plugin is a package somebody chose to `pip install`, and the
 * trust decision sits with a person at install time rather than with a program
 * at open time.
 *
 * The table therefore shows what is installed and what each plugin contributed,
 * and flags the two ways a manifest can be wrong: a contribution the manifest
 * did not declare (refused) and one it declared but never made (missing).
 */

import { type ReactNode } from "react";

import { usePlugins } from "../api/hooks";
import { PageHead } from "../components/Layout";
import { Empty, ErrorNotice, Notice, Panel, Spinner } from "../components/ui";

export function PluginsPage(): ReactNode {
  const plugins = usePlugins();

  if (plugins.isLoading) return <Spinner label="Looking for plugins" />;
  if (plugins.isError) return <ErrorNotice error={plugins.error} />;
  if (!plugins.data) return null;

  const { plugins: installed, contributions, categories, disabled, entry_point_group } =
    plugins.data;
  const broken = installed.filter((plugin) => !plugin.ok);

  return (
    <>
      <PageHead
        title="Plugins"
        subtitle={`${installed.length} installed · discovered through ${entry_point_group}`}
      />

      {disabled && (
        <Notice tone="warn">
          Plugin loading is switched off by <code>CACOPHONY_NO_PLUGINS</code>. A project
          that requires a plugin will refuse to compile.
        </Notice>
      )}

      <Notice>
        Cacophony finds plugins through installed <strong>entry points</strong>, never
        from a directory beside a schema. A project file is something people share, and
        opening one must not be the same as running its author&rsquo;s code. A project
        depends on a plugin by requiring it:
        <pre className="mono" style={{ marginTop: 8, marginBottom: 0, fontSize: "0.78rem" }}>
{`requires:
  plugins: [network_packets]`}
        </pre>
      </Notice>

      {installed.length === 0 ? (
        <Empty title="None installed">
          <p>A plugin is a package that declares itself:</p>
          <pre className="mono" style={{ fontSize: "0.78rem" }}>
{`[project.entry-points."${entry_point_group}"]
network_packets = "my_package:NetworkPackets"`}
          </pre>
          <p className="faint">
            The eight categories of section 44: {categories.join(", ")}.
          </p>
        </Empty>
      ) : (
        <Panel title="Installed">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Plugin</th>
                  <th>Version</th>
                  <th>Declares</th>
                  <th>Registered</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {installed.map((plugin) => (
                  <tr key={plugin.name}>
                    <td>
                      {plugin.name}
                      {plugin.description && (
                        <div className="faint" style={{ fontSize: "0.76rem" }}>
                          {plugin.description}
                        </div>
                      )}
                    </td>
                    <td className="mono">{plugin.version}</td>
                    <td className="faint">{summarise(plugin.provides)}</td>
                    <td className="faint">{summarise(plugin.registered)}</td>
                    <td style={{ color: plugin.ok ? undefined : "var(--red)" }}>
                      {plugin.ok ? "loaded" : "problem"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {broken.length > 0 && (
        <Panel title="Problems">
          {broken.map((plugin) => (
            <div key={plugin.name} style={{ marginBottom: 10 }}>
              <strong>{plugin.name}</strong>
              {plugin.error && <div style={{ color: "var(--red)" }}>{plugin.error}</div>}
              {plugin.refused.map((item) => (
                <div key={item} style={{ color: "var(--red)", fontSize: "0.82rem" }}>
                  refused <code>{item}</code> — it registered something its manifest did
                  not declare
                </div>
              ))}
              {plugin.missing.map((item) => (
                <div key={item} style={{ color: "var(--amber)", fontSize: "0.82rem" }}>
                  missing <code>{item}</code> — declared but never registered
                </div>
              ))}
            </div>
          ))}
          <p className="faint" style={{ fontSize: "0.78rem", marginBottom: 0 }}>
            Neither is a security problem — a plugin is code you installed. Both mean a
            manifest has drifted from its code, which produces a project that works on one
            machine and fails on another.
          </p>
        </Panel>
      )}

      {Object.keys(contributions).length > 0 && (
        <Panel title="What they added">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Category</th>
                  <th>Name</th>
                  <th>From</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(contributions).flatMap(([category, names]) =>
                  Object.entries(names).map(([name, plugin]) => (
                    <tr key={`${category}.${name}`}>
                      <td className="faint">{category}</td>
                      <td className="mono">{name}</td>
                      <td>{plugin}</td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </>
  );
}

function summarise(provides: Record<string, string[]>): string {
  const entries = Object.entries(provides);
  if (entries.length === 0) return "—";
  return entries.map(([category, names]) => `${category}: ${names.join(", ")}`).join(" · ");
}
