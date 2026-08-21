/**
 * The generate screen (design document section 54).
 *
 *     Records                   12,500,000
 *     Estimated text tokens     8.2M
 *     Images                    5,000
 *     Estimated storage         19.4 GB
 *     ...
 *     [ START CACOPHONY ]
 *
 * Section 54 lists what to display before that button: requested scale,
 * estimated workloads, provider requirements, disk estimate, the generation
 * plan and warnings. The estimate reacts to the record override, because an
 * estimate for the schema's declared counts is misleading the moment someone
 * types a different number into the form.
 *
 * The formats and the output layouts come from the server rather than from a
 * list kept here: this screen once offered four of the six registered formats
 * and none of the layouts a project declares, which is what a hand-written
 * list does the first time the registry gains something (sections 33, 34).
 */

import { type ReactNode, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  useLint,
  useOutputs,
  usePlan,
  useProviders,
  useSchema,
  useStartRun,
} from "../api/hooks";
import type {
  CreateRunBody,
  OutputProfileView,
  PlanView,
  ProviderConfig,
  SchemaView,
} from "../api/types";
import { PageHead } from "../components/Layout";
import {
  Empty,
  ErrorNotice,
  GeneratorBadge,
  LintList,
  Notice,
  Panel,
  Spinner,
  Stat,
  formatBytes,
  formatNumber,
} from "../components/ui";
import { useStudio } from "../state/store";

const PROVENANCE = ["none", "run", "record", "field", "full"];
const POLICIES = ["abort", "retry", "skip", "placeholder", "incomplete"];

const CACHE_MODES = ["disabled", "read_only", "read_write"];

/** Section 54's three provider headings, and what each one is called. */
const REQUIREMENTS = [
  { kind: "language_model", label: "LLM" },
  { kind: "image", label: "Images" },
  { kind: "speech", label: "Speech" },
] as const;

export function GeneratePage(): ReactNode {
  const navigate = useNavigate();
  const projectId = useStudio((state) => state.projectId);
  const form = useStudio((state) => state.generate);
  const update = useStudio((state) => state.updateGenerate);

  const schema = useSchema(projectId);
  const plan = usePlan(projectId);
  const lint = useLint(projectId);
  const providers = useProviders(projectId ?? undefined);
  const outputs = useOutputs(projectId ?? undefined);
  const start = useStartRun(projectId ?? -1);

  const override = form.records.trim() === "" ? null : Number(form.records);
  const selected = form.entities.length > 0 ? form.entities : (plan.data?.entity_order ?? []);
  const estimate = useMemo(
    () => scaleEstimate(plan.data, selected, override),
    [plan.data, selected, override],
  );

  if (projectId === null) {
    return (
      <Empty title="No project selected">
        <p>
          Choose one on the <Link to="/projects">Projects</Link> page.
        </p>
      </Empty>
    );
  }

  if (plan.isLoading || schema.isLoading) return <Spinner label="Compiling the plan" />;
  if (plan.isError) return <ErrorNotice error={plan.error} />;
  if (!plan.data || !schema.data) return null;

  const blocking = lint.data?.issues.filter((issue) => issue.severity === "error") ?? [];
  const configured = providers.data?.configured ?? [];
  const formats = outputs.data?.formats ?? [];
  const profiles = outputs.data?.profiles ?? [];
  const format = formats.find((entry) => entry.name === form.outputFormat);
  const profile = profiles.find((entry) => entry.name === form.outputProfile);
  const needed = requiredProviders(schema.data, selected, configured);
  // The plan's memory figure is computed for a default batch. This screen has
  // the batch size and the worker count in front of it, so it does the
  // arithmetic again rather than showing a number about a different run.
  const memory = memoryFor(estimate, form.batchSize, form.workers, selected.length);
  const unmet = needed.filter((requirement) => requirement.serving.length === 0);

  /**
   * Choosing a layout fills the controls it decides rather than hiding them:
   * what a run is about to do should be visible in the form, and an edit
   * afterwards is a deliberate override rather than a contradiction.
   */
  const chooseProfile = (name: string): void => {
    const chosen = profiles.find((entry) => entry.name === name);
    update({
      outputProfile: name,
      ...(chosen
        ? {
            outputDir: chosen.path,
            outputFormat: chosen.format,
            ...(chosen.entities.length > 0 ? { entities: [...chosen.entities] } : {}),
          }
        : {}),
    });
  };

  const submit = () => {
    const body: CreateRunBody = {
      output_dir: form.outputDir,
      output_format: form.outputFormat,
      output_profile: form.outputProfile,
      entities: form.entities,
      records: override,
      seed: form.seed.trim() === "" ? null : Number(form.seed),
      validate: form.validate,
      drop_invalid: form.dropInvalid,
      provenance: form.provenance,
      failure_policy: form.failurePolicy,
      cache_mode: form.cacheMode,
      checkpoint_every: form.checkpointEvery,
      limits: {
        max_workers: form.workers,
        batch_size: form.batchSize,
        llm_batch_size: form.llmBatchSize,
      },
    };
    start.mutate(body, { onSuccess: (run) => navigate(`/runs/${run.id}`) });
  };

  return (
    <>
      <PageHead
        title="Generate"
        subtitle={`${schema.data.name} · seed ${form.seed.trim() || plan.data.seed}`}
      />

      <div className="grid grid-4">
        <Stat label="Records" value={formatNumber(estimate.records)} tone="violet" />
        <Stat
          label="Estimated tokens"
          value={formatNumber(estimate.llm_tokens)}
          note={`${formatNumber(estimate.llm_calls)} model calls`}
          tone="cyan"
        />
        <Stat
          label="Media"
          value={formatNumber(estimate.image_calls + estimate.speech_calls)}
          note="images and audio"
          tone="magenta"
        />
        <Stat
          label="Estimated storage"
          value={formatBytes(estimate.estimated_bytes)}
          note="approximate"
        />
        {/* Section 69 asks for memory as well, and it is the figure that
            decides whether a run of this size is possible on this machine. */}
        <Stat
          label="Peak memory"
          value={formatBytes(memory)}
          note={`${formatNumber(form.batchSize)} records × ${Math.max(
            1,
            Math.min(form.workers, Math.max(1, selected.length)),
          )} at a time`}
        />
      </div>

      <div style={{ height: 16 }} />

      {blocking.length > 0 && (
        <Notice tone="error">
          The linter found {blocking.length} error
          {blocking.length === 1 ? "" : "s"}. A run may still start, but the
          output is unlikely to be what you meant.
        </Notice>
      )}

      {unmet.length > 0 && (
        <Notice tone="warn">
          {unmet.map((requirement) => requirement.label).join(" and ")} generation is
          requested by {unmet.flatMap((requirement) => requirement.fields).length} field
          {unmet.flatMap((requirement) => requirement.fields).length === 1 ? "" : "s"}, and
          the project configures nothing that serves it. Fields with{" "}
          <code>on_unavailable: placeholder</code> will emit marked stand-ins; the rest
          will fail. Configure one on the <Link to="/providers">Providers</Link> page.
        </Notice>
      )}

      {/* The same warning `generate` prints, for the same reason: an enforced
          schema and deliberate damage cannot both be had (section 33). */}
      {outputs.data?.chaos && (format?.name === "sqlite" || format?.name === "sql") && (
        <Notice tone="warn">
          Chaos is enabled, so the tables carry no keys, uniqueness or NOT NULL —
          the damage would be rejected by the constraints it is designed to
          violate. Indexes are still created.
        </Notice>
      )}

      <div className="grid grid-2">
        <Panel title="Run">
          {profiles.length > 0 && (
            <div className="field-row">
              <label htmlFor="out-profile">Layout</label>
              <select
                id="out-profile"
                value={form.outputProfile}
                onChange={(event) => chooseProfile(event.target.value)}
              >
                <option value="">no layout — choose below</option>
                {profiles.map((entry) => (
                  <option key={entry.name} value={entry.name}>
                    {entry.name} · {entry.format}
                  </option>
                ))}
              </select>
              <div className="hint">
                {profile ? (
                  <ProfileSummary profile={profile} />
                ) : (
                  <>One of the layouts this project declares under <code>outputs:</code>.</>
                )}
              </div>
            </div>
          )}

          <div className="field-row">
            <label htmlFor="out-dir">Output directory</label>
            <input
              id="out-dir"
              value={form.outputDir}
              onChange={(event) => update({ outputDir: event.target.value })}
            />
            <div className="hint">
              Resolved on the machine running the server.
              {format?.single_file && (
                <>
                  {" "}
                  Every entity becomes a table in one{" "}
                  <code>cacophony{format.extension}</code> inside it.
                </>
              )}
            </div>
          </div>

          <div className="row" style={{ gap: 10 }}>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="out-format">Format</label>
              <select
                id="out-format"
                value={form.outputFormat}
                onChange={(event) => update({ outputFormat: event.target.value })}
              >
                {/* Offered by the writer registry rather than by a list kept
                    here, which is how the two database formats came to be
                    missing from this menu (sections 33, 54). */}
                {formats.map((entry) => (
                  <option key={entry.name} value={entry.name}>
                    {entry.name}
                  </option>
                ))}
              </select>
              {format && <div className="hint">{format.summary}</div>}
            </div>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="records">Records per entity</label>
              <input
                id="records"
                type="number"
                min={0}
                placeholder="schema default"
                value={form.records}
                onChange={(event) => update({ records: event.target.value })}
              />
            </div>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="seed">Seed</label>
              <input
                id="seed"
                type="number"
                placeholder={String(plan.data.seed)}
                value={form.seed}
                onChange={(event) => update({ seed: event.target.value })}
              />
            </div>
          </div>

          <div className="field-row">
            <label>Entities</label>
            {plan.data.entity_order.map((name) => (
              <label className="checkbox" key={name}>
                <input
                  type="checkbox"
                  checked={form.entities.length === 0 || form.entities.includes(name)}
                  onChange={(event) => {
                    const all = plan.data.entity_order;
                    const current =
                      form.entities.length === 0 ? [...all] : [...form.entities];
                    const next = event.target.checked
                      ? [...new Set([...current, name])]
                      : current.filter((entry) => entry !== name);
                    update({ entities: next.length === all.length ? [] : next });
                  }}
                />
                {name}
                <span className="faint"> · {formatNumber(entityCount(plan.data, name))}</span>
              </label>
            ))}
          </div>
        </Panel>

        <Panel title="Behaviour">
          <div className="row" style={{ gap: 10 }}>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="provenance">Provenance</label>
              <select
                id="provenance"
                value={form.provenance}
                onChange={(event) => update({ provenance: event.target.value })}
              >
                {PROVENANCE.map((mode) => (
                  <option key={mode} value={mode}>
                    {mode}
                  </option>
                ))}
              </select>
              <div className="hint">Costs storage; `full` also keeps prompts.</div>
            </div>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="policy">On failure</label>
              <select
                id="policy"
                value={form.failurePolicy}
                onChange={(event) => update({ failurePolicy: event.target.value })}
              >
                {POLICIES.map((policy) => (
                  <option key={policy} value={policy}>
                    {policy}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="row" style={{ gap: 10 }}>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="cache">Cache</label>
              <select
                id="cache"
                value={form.cacheMode}
                onChange={(event) => update({ cacheMode: event.target.value })}
              >
                {CACHE_MODES.map((mode) => (
                  <option key={mode} value={mode}>
                    {mode}
                  </option>
                ))}
              </select>
            </div>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="workers">Workers</label>
              <input
                id="workers"
                type="number"
                min={1}
                max={32}
                value={form.workers}
                onChange={(event) => update({ workers: Number(event.target.value) })}
              />
            </div>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="batch">Batch size</label>
              <input
                id="batch"
                type="number"
                min={1}
                value={form.batchSize}
                onChange={(event) => update({ batchSize: Number(event.target.value) })}
              />
            </div>
          </div>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={form.validate}
              onChange={(event) => update({ validate: event.target.checked })}
            />
            Validate records
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={form.dropInvalid}
              disabled={!form.validate}
              onChange={(event) => update({ dropInvalid: event.target.checked })}
            />
            Discard records that fail validation
          </label>

          {/* Section 54 asks for provider *requirements*, not an inventory:
              what this run needs, and what is configured to serve it. */}
          {needed.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="panel-title">Provider requirements</div>
              {needed.map((requirement) => (
                <div
                  key={requirement.kind}
                  className="row spread"
                  style={{ fontSize: "0.8rem" }}
                  title={`${requirement.fields.length} field${
                    requirement.fields.length === 1 ? "" : "s"
                  }: ${requirement.fields.join(", ")}`}
                >
                  <span>{requirement.label}</span>
                  {requirement.serving.length > 0 ? (
                    <span className="faint">
                      {requirement.serving
                        .map((provider) =>
                          [provider.id, provider.model].filter(Boolean).join(" / "),
                        )
                        .join(", ")}
                    </span>
                  ) : (
                    <span style={{ color: "var(--red)" }}>none configured</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {needed.length === 0 && configured.length > 0 && (
            <p className="hint" style={{ marginBottom: 0 }}>
              Nothing in this run needs a provider; the {configured.length} configured
              {configured.length === 1 ? " one is" : " ones are"} idle.
            </p>
          )}
        </Panel>
      </div>

      <div style={{ height: 16 }} />

      <Panel title="Generation plan">
        {plan.data.steps.map((step) => (
          <details key={step.entity} style={{ marginBottom: 8 }}>
            <summary style={{ cursor: "pointer" }}>
              <strong>{step.entity}</strong>{" "}
              <span className="faint nums">
                {formatNumber(override ?? step.count)} records · {step.fields.length} fields
              </span>
              {step.depends_on.length > 0 && (
                <span className="faint"> · after {step.depends_on.join(", ")}</span>
              )}
            </summary>
            <div style={{ padding: "8px 0 0 16px" }}>
              {step.fields.map((name) => (
                <div key={name} className="row" style={{ gap: 8, fontSize: "0.8rem" }}>
                  <GeneratorBadge generator={generatorOf(step.generators[name])} />
                  <span>{name}</span>
                  <span className="faint truncate">{step.generators[name]}</span>
                </div>
              ))}
            </div>
          </details>
        ))}
      </Panel>

      <div style={{ height: 16 }} />

      {lint.data && lint.data.issues.length > 0 && (
        <>
          <Panel title="Warnings">
            <LintList issues={lint.data.issues} />
          </Panel>
          <div style={{ height: 16 }} />
        </>
      )}

      <ErrorNotice error={start.error} />

      <button
        type="button"
        className="button-primary"
        style={{ fontSize: "0.95rem", padding: "12px 26px", letterSpacing: "0.06em" }}
        disabled={start.isPending}
        onClick={submit}
      >
        {start.isPending ? "Starting…" : "START CACOPHONY"}
      </button>
    </>
  );
}

/** What a chosen layout will do, said in one line rather than three fields. */
function ProfileSummary({ profile }: { profile: OutputProfileView }): ReactNode {
  return (
    <>
      Writes <code>{profile.format}</code> into <code>{profile.path}</code>
      {profile.partition_by.length > 0 && (
        <>
          , partitioned by <code>{profile.partition_by.join(", ")}</code>
        </>
      )}
      {profile.entities.length > 0 && <> · {profile.entities.join(", ")} only</>}.
    </>
  );
}

interface Requirement {
  kind: string;
  label: string;
  /** Fields that need this kind of provider, as `entity.field`. */
  fields: string[];
  /** Configured providers that can serve it. */
  serving: ProviderConfig[];
}

/**
 * Which kinds of provider this run needs, and what is configured to serve
 * them (section 54's "provider requirements").
 *
 * Read from the fields rather than from the estimate, because "5,000 model
 * calls" does not say whether the project has a language model configured,
 * and that is the question this screen is being asked.
 */
function requiredProviders(
  schema: SchemaView | undefined,
  entities: string[],
  configured: ProviderConfig[] = [],
): Requirement[] {
  if (!schema) return [];
  const chosen = new Set(entities);
  const wanted = new Map<string, string[]>();

  for (const name of schema.entity_order) {
    if (chosen.size > 0 && !chosen.has(name)) continue;
    const entity = schema.entities[name];
    if (!entity) continue;
    for (const field of Object.values(entity.fields)) {
      if (!field.requires_provider) continue;
      const fields = wanted.get(field.requires_provider) ?? [];
      fields.push(`${name}.${field.name}`);
      wanted.set(field.requires_provider, fields);
    }
  }

  return REQUIREMENTS.filter(({ kind }) => wanted.has(kind)).map(({ kind, label }) => ({
    kind,
    label,
    fields: wanted.get(kind) ?? [],
    serving: configured.filter((provider) => provider.type === kind),
  }));
}

/**
 * Peak memory for the batch size and worker count this form is set to.
 *
 * Mirrors `WorkloadEstimate.memory_for`: a batch slot costs `batch_bytes` per
 * record, `--workers` overlaps that many entities, and neither figure grows
 * with the record count (section 31).
 */
function memoryFor(
  estimate: typeof EMPTY_ESTIMATE,
  batchSize: number,
  workers: number,
  entities: number,
): number {
  if (!estimate.batch_bytes) return estimate.peak_memory_bytes;
  const overlap = Math.max(1, Math.min(workers, Math.max(1, entities)));
  return Math.max(1, batchSize) * estimate.batch_bytes * overlap;
}

const EMPTY_ESTIMATE = {
  records: 0,
  fields: 0,
  llm_calls: 0,
  image_calls: 0,
  speech_calls: 0,
  estimated_bytes: 0,
  llm_tokens: 0,
  peak_memory_bytes: 0,
  batch_bytes: 0,
  assumed_batch_size: 0,
  assumed_llm_batch_size: 0,
};

function entityCount(plan: PlanView, entity: string): number {
  return plan.steps.find((step) => step.entity === entity)?.count ?? 0;
}

/**
 * Rescale the plan's estimate for the entities and record count actually
 * selected. Section 69 insists estimates must not pretend to be exact; showing
 * the schema's numbers while the form says something else would be worse than
 * inexact, it would be wrong.
 */
function scaleEstimate(
  plan: PlanView | undefined,
  entities: string[],
  override: number | null,
): typeof EMPTY_ESTIMATE {
  if (!plan) return EMPTY_ESTIMATE;
  const chosen = new Set(entities);

  return plan.steps
    .filter((step) => chosen.size === 0 || chosen.has(step.entity))
    .reduce((total, step) => {
      const factor = override === null || step.count === 0 ? 1 : override / step.count;
      const scaled = override === null ? step.estimate : scale(step.estimate, factor);
      return {
        records: total.records + scaled.records,
        fields: total.fields + scaled.fields,
        llm_calls: total.llm_calls + scaled.llm_calls,
        image_calls: total.image_calls + scaled.image_calls,
        speech_calls: total.speech_calls + scaled.speech_calls,
        estimated_bytes: total.estimated_bytes + scaled.estimated_bytes,
        llm_tokens: total.llm_tokens + scaled.llm_tokens,
        // Entities are written a batch at a time, so the memory a run needs is
        // the largest entity's batch rather than the sum of all of them - the
        // same arithmetic `WorkloadEstimate.merge` does (section 31).
        peak_memory_bytes: Math.max(total.peak_memory_bytes, scaled.peak_memory_bytes),
        batch_bytes: Math.max(total.batch_bytes, scaled.batch_bytes),
        assumed_batch_size: Math.max(total.assumed_batch_size, scaled.assumed_batch_size),
        assumed_llm_batch_size: Math.max(
          total.assumed_llm_batch_size,
          scaled.assumed_llm_batch_size,
        ),
      };
    }, EMPTY_ESTIMATE);
}

function scale(estimate: typeof EMPTY_ESTIMATE, factor: number): typeof EMPTY_ESTIMATE {
  return {
    records: Math.round(estimate.records * factor),
    fields: Math.round(estimate.fields * factor),
    llm_calls: Math.round(estimate.llm_calls * factor),
    image_calls: Math.round(estimate.image_calls * factor),
    speech_calls: Math.round(estimate.speech_calls * factor),
    estimated_bytes: Math.round(estimate.estimated_bytes * factor),
    llm_tokens: Math.round(estimate.llm_tokens * factor),
    // Memory is a batch, and a batch does not grow with the record count.
    peak_memory_bytes: estimate.peak_memory_bytes,
    batch_bytes: estimate.batch_bytes,
    assumed_batch_size: estimate.assumed_batch_size,
    assumed_llm_batch_size: estimate.assumed_llm_batch_size,
  };
}

/** The plan describes generators as `faker(first_name)`; the badge wants `faker`. */
function generatorOf(description: string | undefined): string {
  if (!description) return "rule";
  const match = /^([a-z_]+)/.exec(description);
  return match?.[1] ?? "rule";
}
