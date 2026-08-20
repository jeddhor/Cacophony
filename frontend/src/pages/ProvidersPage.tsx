/**
 * Providers (design document sections 43, 46, 63, 85).
 *
 * Providers are addressed by URI and Cacophony owns no models, so the useful
 * questions here are "is it reachable?" and "what is it serving?" - both of
 * which need an actual request rather than a configuration file. Hence the
 * test button.
 *
 * They are also configured here rather than only in the file. A provider is
 * four lines of YAML that people get wrong once and then avoid, and every edit
 * goes through the same targeted patch the rest of the Studio uses, so the
 * document keeps its comments and a rejected change never reaches it.
 *
 * What is never edited here is a credential: section 63 allows a logical
 * secret id in a project file and nothing else.
 */

import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { usePatchSchema, useProviders, useSchema } from "../api/hooks";
import type { ModelInfo, ProviderHealth, SchemaOperation } from "../api/types";
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

/** What a provider declaration holds, beyond its adapter (section 43). */
const SETTINGS = [
  { key: "base_url", label: "URL", placeholder: "http://localhost:11434" },
  { key: "model", label: "Model", placeholder: "llama3.1:8b" },
  { key: "concurrency", label: "Concurrency", placeholder: "1", numeric: true },
  { key: "timeout_seconds", label: "Timeout (s)", placeholder: "120", numeric: true },
  { key: "secret", label: "Secret id", placeholder: "OPENAI_API_KEY" },
] as const;

export function ProvidersPage(): ReactNode {
  const projectId = useStudio((state) => state.projectId);
  const providers = useProviders(projectId ?? undefined);
  const schema = useSchema(projectId);
  const patch = usePatchSchema(projectId ?? -1);

  const editable = schema.data?.editable ?? false;
  const apply = (operations: SchemaOperation[]) => patch.mutate(operations);

  return (
    <>
      <PageHead
        title="Providers"
        subtitle="Generation backends, addressed by URI. Cacophony never owns the models."
      />

      {providers.isLoading && <Spinner />}
      <ErrorNotice error={providers.error} />
      <ErrorNotice error={patch.error} />

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
          ) : (
            <>
              {editable ? (
                <AddProvider
                  adapters={providers.data.adapters}
                  kinds={providers.data.kinds ?? {}}
                  taken={providers.data.configured.map((provider) => provider.id)}
                  pending={patch.isPending}
                  onAdd={apply}
                />
              ) : (
                <Notice tone="warn">
                  This project has no file to write to, so its providers are
                  read-only. Register it from a path to configure them here.
                </Notice>
              )}

              <div style={{ height: 16 }} />

              {providers.data.configured.length === 0 ? (
                <Empty title="This project configures no providers">
                  <p>
                    Add one above, or under <code>providers:</code> in the schema, to
                    use language-model fields.
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
                      adapters={providers.data.adapters}
                      baseUrl={provider.base_url}
                      model={provider.model}
                      concurrency={provider.concurrency}
                      timeoutSeconds={provider.timeout_seconds}
                      secretId={provider.secret_id}
                      kinds={providers.data.kinds ?? {}}
                      editable={editable}
                      pending={patch.isPending}
                      onPatch={apply}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}

/** A new provider needs a name and an adapter; everything else has a default. */
function AddProvider({
  adapters,
  kinds,
  taken,
  pending,
  onAdd,
}: {
  adapters: string[];
  kinds: Record<string, string>;
  taken: string[];
  pending: boolean;
  onAdd: (operations: SchemaOperation[]) => void;
}): ReactNode {
  const [id, setId] = useState("");
  const [adapter, setAdapter] = useState(adapters.includes("ollama") ? "ollama" : adapters[0] ?? "");

  const clash = taken.includes(id.trim());
  const ready = id.trim() !== "" && adapter !== "" && !clash;

  return (
    <Panel title="Add a provider">
      <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
        <div className="field-row" style={{ flex: 1, marginBottom: 0 }}>
          <label htmlFor="provider-id">Name</label>
          <input
            id="provider-id"
            value={id}
            placeholder="local_llm"
            onChange={(event) => setId(event.target.value)}
          />
        </div>
        <div className="field-row" style={{ flex: 1, marginBottom: 0 }}>
          <label htmlFor="provider-adapter">Adapter</label>
          <select
            id="provider-adapter"
            value={adapter}
            onChange={(event) => setAdapter(event.target.value)}
          >
            {adapters.map((name) => (
              <option key={name} value={name}>
                {name} · {(kinds[name] ?? "custom").replace("_", " ")}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          className="button-primary"
          disabled={!ready || pending}
          onClick={() => {
            onAdd([
              {
                op: "add_provider",
                name: id.trim(),
                // The adapter decides what kind of provider this is, so the
                // declaration says so rather than leaving it to be inferred.
                value: { adapter, type: kinds[adapter] ?? "custom" },
              },
            ]);
            setId("");
          }}
        >
          Add
        </button>
      </div>
      <p className="hint" style={{ marginBottom: 0 }}>
        {clash
          ? `This project already has a provider called ${id.trim()}.`
          : "The name is what a field's provider: option refers to. Set its URL and model on the card that appears."}
      </p>
    </Panel>
  );
}

function ProviderCard({
  projectId,
  id,
  adapter,
  adapters,
  kinds,
  baseUrl,
  model,
  concurrency,
  timeoutSeconds,
  secretId,
  editable,
  pending,
  onPatch,
}: {
  projectId: number;
  id: string;
  adapter: string;
  adapters: string[];
  kinds: Record<string, string>;
  baseUrl: string | null;
  model: string | null;
  concurrency: number;
  timeoutSeconds: number;
  secretId: string | null;
  editable: boolean;
  pending: boolean;
  onPatch: (operations: SchemaOperation[]) => void;
}): ReactNode {
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const current: Record<string, string> = {
    base_url: baseUrl ?? "",
    model: model ?? "",
    concurrency: String(concurrency),
    timeout_seconds: String(timeoutSeconds),
    secret: secretId ?? "",
  };

  const set = (key: string, value: unknown) =>
    onPatch([{ op: "set_provider", name: id, key, value }]);

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
        <>
          <button type="button" className="button-sm" onClick={probe} disabled={busy}>
            {busy ? "Testing…" : "Test"}
          </button>
          {editable && (
            <button
              type="button"
              className="button-sm"
              disabled={pending}
              onClick={() => {
                if (window.confirm(`Remove the provider ${id}?`)) {
                  onPatch([{ op: "remove_provider", name: id }]);
                }
              }}
            >
              Remove
            </button>
          )}
        </>
      }
    >
      <div className="field-row">
        <label htmlFor={`adapter-${id}`}>Adapter</label>
        <select
          id={`adapter-${id}`}
          value={adapter}
          disabled={!editable}
          onChange={(event) => {
            // `type` is only ever read by people, but a provider labelled a
            // language model while serving images is a lie in the one place
            // someone would look to check. Both change, as one patch.
            const chosen = event.target.value;
            onPatch([
              { op: "set_provider", name: id, key: "adapter", value: chosen },
              {
                op: "set_provider",
                name: id,
                key: "type",
                value: kinds[chosen] ?? "custom",
              },
            ]);
          }}
        >
          {adapters.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      {SETTINGS.map((setting) => (
        <div className="field-row" key={setting.key}>
          <label htmlFor={`${setting.key}-${id}`}>{setting.label}</label>
          <input
            id={`${setting.key}-${id}`}
            // Keyed on the stored value so a rejected or reverted edit shows
            // what the file actually says rather than what was typed.
            key={`${id}-${setting.key}-${current[setting.key]}`}
            defaultValue={current[setting.key]}
            placeholder={setting.placeholder}
            readOnly={!editable}
            className={setting.key === "base_url" ? "mono" : undefined}
            onBlur={(event) => {
              const raw = event.target.value.trim();
              if (raw === current[setting.key]) return;
              const numeric = "numeric" in setting && setting.numeric;
              set(setting.key, raw === "" ? null : numeric ? Number(raw) : raw);
            }}
          />
        </div>
      ))}

      <p className="hint">
        The secret id names a credential; it is never the credential. Cacophony
        resolves it from the OS keychain, the environment or an encrypted store
        when the run starts <span className="faint">(section 63)</span>.
      </p>

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
