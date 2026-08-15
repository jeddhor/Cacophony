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
 */

import { type ReactNode, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useLint, usePlan, useProviders, useSchema, useStartRun } from "../api/hooks";
import type { CreateRunBody, PlanView } from "../api/types";
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

const FORMATS = ["jsonl", "csv", "json", "parquet"];
const PROVENANCE = ["none", "run", "record", "field", "full"];
const POLICIES = ["abort", "retry", "skip", "placeholder", "incomplete"];
const CACHE_MODES = ["disabled", "read_only", "read_write"];

export function GeneratePage(): ReactNode {
  const navigate = useNavigate();
  const projectId = useStudio((state) => state.projectId);
  const form = useStudio((state) => state.generate);
  const update = useStudio((state) => state.updateGenerate);

  const schema = useSchema(projectId);
  const plan = usePlan(projectId);
  const lint = useLint(projectId);
  const providers = useProviders(projectId ?? undefined);
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
  const needsProvider = estimate.llm_calls > 0 || estimate.image_calls > 0;
  const configured = providers.data?.configured ?? [];

  const submit = () => {
    const body: CreateRunBody = {
      output_dir: form.outputDir,
      output_format: form.outputFormat,
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
          value={formatNumber(estimate.llm_calls * 180)}
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
      </div>

      <div style={{ height: 16 }} />

      {blocking.length > 0 && (
        <Notice tone="error">
          The linter found {blocking.length} error
          {blocking.length === 1 ? "" : "s"}. A run may still start, but the
          output is unlikely to be what you meant.
        </Notice>
      )}

      {needsProvider && configured.length === 0 && (
        <Notice tone="warn">
          This run needs a generation backend, but the project configures none.
          Fields with <code>on_unavailable: placeholder</code> will emit marked
          stand-ins; the rest will fail.
        </Notice>
      )}

      <div className="grid grid-2">
        <Panel title="Run">
          <div className="field-row">
            <label htmlFor="out-dir">Output directory</label>
            <input
              id="out-dir"
              value={form.outputDir}
              onChange={(event) => update({ outputDir: event.target.value })}
            />
            <div className="hint">Resolved on the machine running the server.</div>
          </div>

          <div className="row" style={{ gap: 10 }}>
            <div className="field-row" style={{ flex: 1 }}>
              <label htmlFor="out-format">Format</label>
              <select
                id="out-format"
                value={form.outputFormat}
                onChange={(event) => update({ outputFormat: event.target.value })}
              >
                {FORMATS.map((format) => (
                  <option key={format} value={format}>
                    {format}
                  </option>
                ))}
              </select>
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

          {configured.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="panel-title">Providers</div>
              {configured.map((provider) => (
                <div key={provider.id} className="row spread" style={{ fontSize: "0.8rem" }}>
                  <span>{provider.id}</span>
                  <span className="faint">
                    {provider.adapter}
                    {provider.model ? ` · ${provider.model}` : ""}
                  </span>
                </div>
              ))}
            </div>
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

const EMPTY_ESTIMATE = {
  records: 0,
  fields: 0,
  llm_calls: 0,
  image_calls: 0,
  speech_calls: 0,
  estimated_bytes: 0,
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
  };
}

/** The plan describes generators as `faker(first_name)`; the badge wants `faker`. */
function generatorOf(description: string | undefined): string {
  if (!description) return "rule";
  const match = /^([a-z_]+)/.exec(description);
  return match?.[1] ?? "rule";
}
