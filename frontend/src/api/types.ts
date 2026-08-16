/**
 * The shapes the Cacophony API returns (design document section 36).
 *
 * Written by hand rather than generated from the OpenAPI document, because a
 * generated client would reintroduce every `any` the backend's `dict[str,
 * Any]` fields imply. These describe what the routes actually send, and the
 * Studio is type-checked against them.
 */

export type RunState =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export type JobState = RunState | "retrying";

export interface ProjectSummary {
  id: number;
  name: string;
  path: string | null;
  description: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SchemaRevision {
  id: number;
  project_id: number;
  version: number;
  source_hash: string;
  source_format: string;
  summary: {
    name?: string;
    total_records?: number;
    entities?: Record<string, { count: number; fields: number }>;
    providers?: string[];
  };
  created_at: string | null;
  source_text?: string;
}

export interface ProjectDetail extends ProjectSummary {
  revisions: SchemaRevision[];
  run_count: number;
  revision_id?: number;
}

/** Field constraints, as declared in the schema. */
export interface Constraints {
  min?: number | string;
  max?: number | string;
  min_length?: number;
  max_length?: number;
  pattern?: string;
  enum?: unknown[];
  forbidden?: unknown[];
  multiple_of?: number;
  precision?: number;
}

export interface FieldView {
  name: string;
  type: string;
  semantic: string | null;
  description: string | null;
  generator: string;
  generator_describe: string;
  generator_options: Record<string, unknown>;
  /** Whether the recommendation engine chose this generator (section 68). */
  inferred: boolean;
  requires_provider: string | null;
  deterministic: boolean;
  dependencies: string[];
  related_entities: string[];
  nullable: boolean;
  null_probability: number;
  unique: boolean;
  primary_key: boolean;
  tone: string | null;
  constraints: Constraints;
  /** Normalised weights, for the distribution preview (section 52). */
  distribution: Record<string, number> | null;
  /** Where this field points, if it is a foreign key (section 15). */
  reference: FieldReference | null;
}

export interface FieldReference {
  entity: string;
  field: string | null;
  distribution: string | null;
  unique: boolean;
}

/** One foreign key, as an edge of the relationship graph. */
export interface ReferenceEdge {
  from_entity: string;
  from_field: string;
  to_entity: string;
  to_field: string | null;
  distribution: string | null;
  unique: boolean;
}

export interface EntityView {
  name: string;
  count: number;
  description: string | null;
  primary_key: string | null;
  depends_on: string[];
  layers: string[][];
  field_order: string[];
  fields: Record<string, FieldView>;
}

export interface Relationship {
  name: string;
  from: string;
  to: string;
  cardinality: string;
  field: string | null;
  required: boolean;
}

export interface SchemaView {
  project_id: number;
  name: string;
  revision_id: number | null;
  source: string;
  source_format: string;
  /** False for projects registered from inline source: nowhere to save to. */
  editable: boolean;
  project: Record<string, unknown>;
  entity_order: string[];
  entities: Record<string, EntityView>;
  relationships: Relationship[];
  /** Every foreign key in the project. The graph draws these. */
  references: ReferenceEdge[];
}

export interface PlanStep {
  entity: string;
  count: number;
  fields: string[];
  generators: Record<string, string>;
  depends_on: string[];
  estimate: WorkloadEstimate;
}

export interface WorkloadEstimate {
  records: number;
  fields: number;
  llm_calls: number;
  image_calls: number;
  speech_calls: number;
  estimated_bytes: number;
}

export interface PlanView {
  project: string;
  revision_id: number | null;
  seed: number;
  entity_order: string[];
  steps: PlanStep[];
  estimate: WorkloadEstimate;
  warnings: string[];
}

export interface LintIssue {
  code: string;
  severity: "info" | "warning" | "error";
  location: string;
  message: string;
  hint: string | null;
}

export interface LintReport {
  ok: boolean;
  issues: LintIssue[];
}

export interface PreviewResult {
  entity: string;
  columns: string[];
  /** Which generator produced each column (section 51). */
  sources: Record<string, string>;
  records: Record<string, unknown>[];
}

export interface JobView {
  id: number;
  run_id: string;
  sequence: number;
  type: string;
  entity: string | null;
  state: JobState;
  offset: number;
  requested: number;
  completed: number;
  remaining: number;
  progress: number;
  attempts: number;
  part: number;
  depends_on: string[];
  outputs: string[];
  checkpoint: Record<string, unknown>;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  checkpointed_at: string | null;
}

export interface EntityMetrics {
  entity: string;
  requested: number;
  generated: number;
  written: number;
  rejected: number;
  repaired: number;
  field_failures: number;
  remaining: number;
  progress: number;
  records_per_second: number;
}

/** The live figures section 55 wants shown while a run happens. */
export interface RunSnapshot {
  run_id: string;
  progress: number;
  records_written: number;
  records_requested: number;
  records_per_second: number;
  mean_records_per_second: number;
  tokens_per_second: number;
  elapsed_seconds: number;
  eta_seconds: number | null;
  bytes_written: number;
  provider_calls: number;
  provider_errors: number;
  provider_latency_ms: number;
  retries: number;
  validation_failures: number;
  cache_hits: number;
  cache_misses: number;
  queue_depth: number;
  entities: Record<string, EntityMetrics>;
  quality?: Record<string, number>;
  files?: string[];
}

export interface RunStatistic {
  scope: string;
  name: string;
  value: number | null;
  detail: Record<string, unknown>;
}

export interface RunView {
  id: string;
  project_id: number;
  revision_id: number | null;
  state: RunState;
  seed: number;
  output_dir: string | null;
  output_format: string;
  records_requested: number;
  records_written: number;
  progress: number;
  duration_seconds: number | null;
  config: Record<string, unknown>;
  estimate: Partial<WorkloadEstimate>;
  summary: Partial<RunSnapshot>;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  jobs?: JobView[];
  statistics?: RunStatistic[];
  /** Present only while the run is executing in the server process. */
  live?: RunSnapshot;
  paused?: boolean;
}

/** How closely one field's output matched its declared distribution. */
export interface DistributionCheck {
  entity: string;
  field: string;
  samples: number;
  match: number;
  distance: number;
  confident: boolean;
  expected: Record<string, number>;
  observed: Record<string, number>;
}

export interface EntityValidation {
  records_checked: number;
  records_valid: number;
  records_repaired: number;
  records_rejected: number;
  validity_rate: number;
  issues_by_category: Record<string, number>;
  referential?: {
    references_checked: number;
    broken_references: number;
    integrity: number;
    sample_every: number;
  };
  statistical?: {
    samples: number;
    fields_checked: number;
    distribution_match: number;
    checks: DistributionCheck[];
  };
}

export interface ResolverStats {
  key_lookups: number;
  key_hit_rate: number;
  record_lookups: number;
  record_hit_rate: number;
  derived_parent_records: number;
}

/** `GET /api/runs/{id}/quality` - the report of section 58. */
export interface QualityReport {
  run_id: string;
  state: RunState;
  /** True when the numbers come from a run still in progress. */
  live: boolean;
  records: number;
  quality: Record<string, number>;
  validation: Record<string, EntityValidation>;
  relations: ResolverStats | null;
  providers: Record<string, unknown> | null;
}

export interface RunEvent {
  kind: string;
  run_id: string;
  job_id: number | null;
  entity: string | null;
  level: string;
  message: string;
  data: Record<string, unknown> & Partial<RunSnapshot>;
  timestamp: number;
}

export interface StoredEvent {
  id: number;
  run_id: string;
  job_id: number | null;
  timestamp: string;
  level: string;
  event: string;
  entity: string | null;
  message: string;
  data: Record<string, unknown>;
}

export interface ProviderConfig {
  id: string;
  type: string;
  adapter: string;
  base_url: string | null;
  model: string | null;
  concurrency: number;
  secret_id: string | null;
}

export interface ProvidersView {
  adapters: string[];
  aliases: Record<string, string>;
  configured: ProviderConfig[];
}

export interface ProviderHealth {
  id: string;
  healthy: boolean;
  message: string;
  latency_ms: number | null;
  details: Record<string, unknown>;
}

export interface ModelInfo {
  name: string;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  context_length: number | null;
}

export interface GeneratorInfo {
  name: string;
  aliases: string[];
  deterministic: boolean;
  requires_provider: string | null;
  cost_class: string;
  summary: string;
}

export interface SchemaTypesView {
  types: {
    value: string;
    numeric: boolean;
    textual: boolean;
    temporal: boolean;
    media: boolean;
  }[];
  generators: GeneratorInfo[];
  provenance: string[];
  profiles: string[];
}

export interface SystemInfo {
  version: string;
  store: { path: string; schema_version: number };
  active_runs: string[];
  projects: number;
  revisions: number;
  runs: number;
  jobs: number;
  events: number;
}

/** One targeted schema edit (design document section 48). */
export interface SchemaOperation {
  op:
    | "set_project"
    | "set_entity"
    | "add_entity"
    | "remove_entity"
    | "set_field"
    | "unset_field"
    | "add_field"
    | "remove_field"
    | "rename_field"
    | "move_field";
  entity?: string;
  field?: string;
  key?: string;
  value?: unknown;
  name?: string;
  index?: number;
}

export interface CreateRunBody {
  output_dir: string;
  output_format: string;
  entities?: string[];
  records?: number | null;
  seed?: number | null;
  validate?: boolean;
  drop_invalid?: boolean;
  provenance?: string;
  failure_policy?: string;
  cache_mode?: string;
  cache_path?: string | null;
  checkpoint_every?: number;
  limits?: {
    max_workers?: number;
    batch_size?: number;
    llm_batch_size?: number;
    min_free_disk_mb?: number;
    max_retries?: number;
  };
}
