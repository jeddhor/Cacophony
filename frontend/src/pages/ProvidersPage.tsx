/**
 * Providers (design document sections 43, 46, 85).
 *
 * Providers are addressed by URI and Cacophony owns no models, so the useful
 * questions here are "is it reachable?" and "what is it serving?" - both of
 * which need an actual request rather than a configuration file. Hence the
 * test button.
 */

import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useProviders } from "../api/hooks";
import type { ModelInfo, ProviderHealth } from "../api/types";
import { PageHead } from "../components/Layout";
import { Empty, ErrorNotice, Notice, Panel, Spinner } from "../components/ui";
import { useStudio } from "../state/store";

/** The order the headings appear in, and what to call each one. */
const ADAPTER_KINDS = [
  { key: "language_model", label: "Language models" },
  { key: "image", label: "Images" },
  { key: "speech", label: "Speech" },
  { key: "custom", label: "Other" },
] as const;

export function ProvidersPage(): ReactNode {
  const projectId = useStudio((state) => state.projectId);
  const providers = useProviders(projectId ?? undefined);

  return (
    <>
      <PageHead
        title="Providers"
        subtitle="Generation backends, addressed by URI. Cacophony never owns the models."
      />

      {providers.isLoading && <Spinner />}
      <ErrorNotice error={providers.error} />

      {providers.data && (
        <>
          <Panel title="Available adapters">
            {ADAPTER_KINDS.map(({ key, label }) => {
              const names = providers.data.adapters.filter(
                (adapter) => (providers.data.kinds?.[adapter] ?? "custom") === key,
              );
              if (names.length === 0) return null;
              return (
                <div key={key} style={{ marginBottom: 10 }}>
                  <p
                    className="faint"
                    style={{ fontSize: "0.72rem", textTransform: "uppercase", margin: "0 0 4px" }}
                  >
                    {label}
                  </p>
                  <div className="row" style={{ gap: 8 }}>
                    {names.map((adapter) => (
                      <span key={adapter} className="badge badge-faker">
                        {adapter}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}
            <p className="faint" style={{ fontSize: "0.78rem", marginBottom: 0 }}>
              A provider names one of these under <code>providers:</code>. The two
              procedural adapters need no server and no GPU, so a multimodal schema
              can be designed and previewed anywhere.
            </p>
          </Panel>

          <div style={{ height: 16 }} />

          {projectId === null ? (
            <Empty title="No project selected">
              <p>
                Choose one on the <Link to="/projects">Projects</Link> page to see
                its configured providers.
              </p>
            </Empty>
          ) : providers.data.configured.length === 0 ? (
            <Empty title="This project configures no providers">
              <p>
                Add one under <code>providers:</code> in the schema to use
                language-model fields.
              </p>
            </Empty>
          ) : (
            <div className="grid grid-2">
              {providers.data.configured.map((provider) => (
                <ProviderCard
                  key={provider.id}
                  projectId={projectId}
                  id={provider.id}
                  adapter={provider.adapter}
                  baseUrl={provider.base_url}
                  model={provider.model}
                  concurrency={provider.concurrency}
                  secretId={provider.secret_id}
                />
              ))}
            </div>
          )}
        </>
      )}
    </>
  );
}

function ProviderCard({
  projectId,
  id,
  adapter,
  baseUrl,
  model,
  concurrency,
  secretId,
}: {
  projectId: number;
  id: string;
  adapter: string;
  baseUrl: string | null;
  model: string | null;
  concurrency: number;
  secretId: string | null;
}): ReactNode {
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const probe = async () => {
    setBusy(true);
    setError(null);
    try {
      const status = await api.testProvider(id, projectId);
      setHealth(status);
      if (status.healthy) {
        setModels(await api.providerModels(id, projectId).catch(() => null));
      }
    } catch (cause) {
      setError(cause);
      setHealth(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title={id}
      actions={
        <button type="button" className="button-sm" onClick={probe} disabled={busy}>
          {busy ? "Testing…" : "Test"}
        </button>
      }
    >
      <table>
        <tbody>
          <tr>
            <td className="faint">Adapter</td>
            <td style={{ textAlign: "right" }}>{adapter}</td>
          </tr>
          <tr>
            <td className="faint">URL</td>
            <td style={{ textAlign: "right" }} className="mono">
              {baseUrl ?? "in process"}
            </td>
          </tr>
          <tr>
            <td className="faint">Model</td>
            <td style={{ textAlign: "right" }}>{model ?? "—"}</td>
          </tr>
          <tr>
            <td className="faint">Concurrency</td>
            <td style={{ textAlign: "right" }}>{concurrency}</td>
          </tr>
          {secretId && (
            <tr>
              <td className="faint">Secret id</td>
              <td style={{ textAlign: "right" }} className="mono">
                {secretId}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div style={{ marginTop: 12 }}>
        <ErrorNotice error={error} />
        {health && (
          <Notice tone={health.healthy ? "info" : "error"}>
            {health.healthy ? "reachable" : "unreachable"} — {health.message}
            {health.latency_ms !== null && <> ({health.latency_ms.toFixed(0)} ms)</>}
          </Notice>
        )}
        {models && models.length > 0 && (
          <>
            <div className="panel-title">Models</div>
            {models.map((info) => (
              <div key={info.name} className="row spread" style={{ fontSize: "0.8rem" }}>
                <span className="mono">{info.name}</span>
                <span className="faint">
                  {[info.parameter_size, info.quantization].filter(Boolean).join(" · ")}
                </span>
              </div>
            ))}
          </>
        )}
      </div>
    </Panel>
  );
}
