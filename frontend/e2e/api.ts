/**
 * A backend, in the sense a layout test needs one.
 *
 * Every route the Studio calls while rendering a page, answered with a payload
 * shaped like the real one. Nothing here generates data - the point is to put
 * enough on the screen that the layout has something to get wrong: long paths,
 * wide tables, nine navigation destinations and a schema with more columns
 * than fit.
 */

import type { Page } from "@playwright/test";

const ENTITIES = ["employee", "device", "location", "login_event", "security_alert"];

/** A field list wide enough that the table must scroll rather than the page. */
function fields(entity: string) {
  const names = [
    "identifier",
    "given_name",
    "family_name",
    "department",
    "electronic_mail_address",
    "office_location_name",
    "employment_started_on",
    "biography",
  ];
  return Object.fromEntries(
    names.map((name) => [
      name,
      {
        name,
        type: name === "biography" ? "text" : "string",
        semantic: name === "biography" ? "A short professional biography." : null,
        description: null,
        generator: name === "biography" ? "llm" : "faker",
        generator_describe:
          name === "biography" ? "llm(per_record)" : `faker(${name})`,
        generator_options: name === "biography" ? { mode: "per_record" } : { provider: name },
        inferred: name === "given_name",
        requires_provider: name === "biography" ? "language_model" : null,
        deterministic: name !== "biography",
        dependencies: name === "electronic_mail_address" ? ["given_name"] : [],
        related_entities: [],
        nullable: false,
        null_probability: 0,
        unique: name === "identifier",
        primary_key: name === "identifier",
        tone: null,
        constraints: {},
        distribution: null,
        reference: null,
        recipe: null,
      },
    ]),
  );
}

const SCHEMA = {
  project_id: 1,
  name: "Corporate Directory",
  revision_id: 3,
  source: "project:\n  name: Corporate Directory\n",
  source_format: "yaml",
  editable: true,
  project: {},
  entity_order: ENTITIES,
  entities: Object.fromEntries(
    ENTITIES.map((entity) => [
      entity,
      {
        name: entity,
        count: 5000,
        description: "Records of a kind.",
        primary_key: "identifier",
        depends_on: [],
        layers: [["identifier"]],
        field_order: Object.keys(fields(entity)),
        fields: fields(entity),
      },
    ]),
  ),
  relationships: [],
  references: [],
};

const ESTIMATE = {
  records: 25_000,
  fields: 200_000,
  llm_calls: 5_000,
  image_calls: 0,
  speech_calls: 0,
  estimated_bytes: 4_200_000,
  llm_tokens: 1_100_000,
  peak_memory_bytes: 12_000_000,
};

const PLAN = {
  project: "Corporate Directory",
  revision_id: 3,
  seed: 42069,
  entity_order: ENTITIES,
  steps: ENTITIES.map((entity) => ({
    entity,
    count: 5000,
    fields: Object.keys(fields(entity)),
    generators: Object.fromEntries(
      Object.keys(fields(entity)).map((name) => [name, `faker(${name})`]),
    ),
    depends_on: [],
    estimate: ESTIMATE,
  })),
  estimate: ESTIMATE,
  warnings: [],
};

const PAYLOADS: Record<string, unknown> = {
  "/api/system": {
    version: "0.1.0",
    store: {
      path: "/home/someone/.local/share/cacophony/store.db",
      schema_version: 4,
    },
    active_runs: [],
    projects: 1,
    revisions: 3,
    runs: 2,
    jobs: 8,
    events: 40,
  },
  "/api/projects": [
    {
      id: 1,
      name: "Corporate Directory",
      path: "/home/someone/projects/corporate-directory.yaml",
      description: "Employees, departments, locations and devices.",
      created_at: "2026-01-05T10:00:00Z",
      updated_at: "2026-01-06T10:00:00Z",
    },
  ],
  "/api/projects/1": {
    id: 1,
    name: "Corporate Directory",
    path: "/home/someone/projects/corporate-directory.yaml",
    description: "Employees, departments, locations and devices.",
    created_at: "2026-01-05T10:00:00Z",
    updated_at: "2026-01-06T10:00:00Z",
    revisions: [],
    run_count: 2,
    revision_id: 3,
  },
  "/api/projects/1/schema": SCHEMA,
  "/api/projects/1/plan": PLAN,
  "/api/projects/1/lint": { ok: true, issues: [] },
  "/api/providers": {
    adapters: ["mock", "ollama", "procedural_image", "procedural_speech"],
    aliases: {},
    kinds: {
      mock: "language_model",
      ollama: "language_model",
      procedural_image: "image",
      procedural_speech: "speech",
    },
    configured: [
      {
        id: "local_llm",
        type: "language_model",
        adapter: "ollama",
        base_url: "http://localhost:11434",
        model: "llama3.1:8b",
        concurrency: 4,
        timeout_seconds: 120,
        secret_id: null,
        options: {},
      },
    ],
  },
  "/api/outputs": {
    formats: [
      {
        name: "jsonl",
        extension: ".jsonl",
        aliases: ["ndjson"],
        single_file: false,
        partitionable: true,
        summary: "One JSON object per line - the default for large datasets.",
      },
      {
        name: "parquet",
        extension: ".parquet",
        aliases: [],
        single_file: false,
        partitionable: true,
        summary: "Apache Parquet, written incrementally with PyArrow.",
      },
      {
        name: "sqlite",
        extension: ".db",
        aliases: [],
        single_file: true,
        partitionable: false,
        summary: "Write an entity into a table of a SQLite database.",
      },
    ],
    profiles: [
      {
        name: "analytics",
        format: "parquet",
        path: "out/corporate-analytics",
        entities: [],
        partition_by: ["department"],
        options: {},
      },
    ],
    chaos: false,
  },
  "/api/runs": [
    {
      id: "6f1c2a20-0000-4000-8000-000000000001",
      project_id: 1,
      revision_id: 3,
      state: "completed",
      seed: 42069,
      output_dir: "/home/someone/datasets/corporate-directory/2026-01-06",
      output_format: "jsonl",
      records_requested: 25_000,
      records_written: 25_000,
      progress: 1,
      duration_seconds: 42.5,
      config: {},
      estimate: ESTIMATE,
      summary: {},
      error: null,
      created_at: "2026-01-06T10:00:00Z",
      started_at: "2026-01-06T10:00:01Z",
      finished_at: "2026-01-06T10:00:44Z",
    },
  ],
  "/api/streams": [],
  "/api/runs/6f1c2a20-0000-4000-8000-000000000001/assets": {
    run_id: "6f1c2a20-0000-4000-8000-000000000001",
    root: "/home/someone/datasets/corporate-directory/2026-01-06/assets",
    total: 0,
    kinds: [],
    entities: [],
    assets: [],
  },
  "/api/generators": [],
  "/api/plugins": {
    loaded: true,
    disabled: false,
    entry_point_group: "cacophony.plugins",
    categories: ["generators", "providers", "validators"],
    plugins: [],
    contributions: {},
  },
  "/api/schema/types": { types: [], generators: [], semantic: [] },
};

/**
 * Answer every API call from the table above, so a page renders its populated
 * state rather than its empty one. Anything unlisted gets an empty object,
 * which is enough for a route the page only polls.
 */
export async function stubApi(page: Page): Promise<void> {
  // Matched on the path rather than with a glob: `**/api/**` also catches the
  // dev server's own `/src/api/client.ts`, and answering a module request with
  // JSON produces a blank page and a MIME-type error.
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      const body = PAYLOADS[path] ?? {};
      await route.fulfill({ json: body });
    },
  );
}

/** The selected project lives in local storage, as it does for a real user. */
export async function selectProject(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "cacophony.studio",
      JSON.stringify({ state: { projectId: 1 }, version: 0 }),
    );
  });
}
