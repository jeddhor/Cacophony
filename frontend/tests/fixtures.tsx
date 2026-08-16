/**
 * Test scaffolding: a render helper and realistic API payloads.
 *
 * The fixtures are shaped exactly as the backend sends them, so a test that
 * passes here is testing the Studio against the API rather than against a
 * convenient invention.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import type {
  EntityView,
  FieldView,
  PlanView,
  PreviewResult,
  RunView,
  SchemaView,
} from "../src/api/types";

export function renderWithProviders(
  ui: ReactElement,
  { route = "/", ...options }: RenderOptions & { route?: string } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }

  return { client, ...render(ui, { wrapper: Wrapper, ...options }) };
}

export function makeField(overrides: Partial<FieldView> = {}): FieldView {
  return {
    name: "field",
    type: "string",
    semantic: null,
    description: null,
    generator: "constant",
    generator_describe: "constant('x')",
    generator_options: {},
    inferred: false,
    requires_provider: null,
    deterministic: true,
    dependencies: [],
    related_entities: [],
    nullable: false,
    null_probability: 0,
    unique: false,
    primary_key: false,
    tone: null,
    constraints: {},
    distribution: null,
    reference: null,
    ...overrides,
  };
}

export const employeeFields: Record<string, FieldView> = {
  employee_id: makeField({
    name: "employee_id",
    generator: "sequence",
    generator_describe: "sequence(EMP-{000000})",
    generator_options: { format: "EMP-{000000}" },
    primary_key: true,
    unique: true,
  }),
  first_name: makeField({
    name: "first_name",
    generator: "faker",
    generator_describe: "faker(first_name)",
    generator_options: { provider: "first_name" },
    semantic: "Person's given name",
    inferred: true,
  }),
  department: makeField({
    name: "department",
    type: "enum",
    generator: "weighted",
    generator_describe: "weighted(Engineering, Sales, +1 more)",
    distribution: { Engineering: 0.5, Sales: 0.3, Finance: 0.2 },
  }),
  email: makeField({
    name: "email",
    type: "email",
    generator: "template",
    generator_describe: "template({first_name|lower}@example.com)",
    dependencies: ["first_name"],
  }),
  biography: makeField({
    name: "biography",
    type: "text",
    generator: "llm",
    generator_describe: "llm(per_record)",
    semantic: "A short professional biography.",
    requires_provider: "language_model",
    deterministic: false,
    dependencies: ["department"],
    constraints: { max_length: 400 },
  }),
};

export const employeeEntity: EntityView = {
  name: "employee",
  count: 5000,
  description: "A person employed by the company.",
  primary_key: "employee_id",
  depends_on: [],
  layers: [["employee_id", "first_name", "department"], ["email", "biography"]],
  field_order: ["employee_id", "first_name", "department", "email", "biography"],
  fields: employeeFields,
};

export const deviceEntity: EntityView = {
  name: "device",
  count: 6200,
  description: null,
  primary_key: "asset_tag",
  depends_on: ["employee"],
  layers: [["asset_tag"]],
  field_order: ["asset_tag"],
  fields: {
    asset_tag: makeField({ name: "asset_tag", generator: "sequence" }),
  },
};

export const schemaFixture: SchemaView = {
  project_id: 1,
  name: "Corporate Directory",
  revision_id: 3,
  source: "project:\n  name: Corporate Directory\n",
  source_format: "yaml",
  editable: true,
  project: {},
  entity_order: ["employee", "device"],
  entities: { employee: employeeEntity, device: deviceEntity },
  relationships: [
    {
      name: "employee_device",
      from: "employee",
      to: "device",
      cardinality: "one_to_many",
      field: null,
      required: true,
    },
  ],
  references: [
    {
      from_entity: "device",
      from_field: "owner",
      to_entity: "employee",
      to_field: "employee_id",
      distribution: "skewed",
      unique: false,
    },
  ],
};

export const planFixture: PlanView = {
  project: "Corporate Directory",
  revision_id: 3,
  seed: 42069,
  entity_order: ["employee", "device"],
  steps: [
    {
      entity: "employee",
      count: 5000,
      fields: ["employee_id", "first_name", "biography"],
      generators: {
        employee_id: "sequence(EMP-{000000})",
        first_name: "faker(first_name)",
        biography: "llm(per_record)",
      },
      depends_on: [],
      estimate: {
        records: 5000,
        fields: 15000,
        llm_calls: 5000,
        image_calls: 0,
        speech_calls: 0,
        estimated_bytes: 4_000_000,
      },
    },
    {
      entity: "device",
      count: 6200,
      fields: ["asset_tag"],
      generators: { asset_tag: "sequence(AST-{00000})" },
      depends_on: ["employee"],
      estimate: {
        records: 6200,
        fields: 6200,
        llm_calls: 0,
        image_calls: 0,
        speech_calls: 0,
        estimated_bytes: 200_000,
      },
    },
  ],
  estimate: {
    records: 11200,
    fields: 21200,
    llm_calls: 5000,
    image_calls: 0,
    speech_calls: 0,
    estimated_bytes: 4_200_000,
  },
  warnings: [],
};

export const previewFixture: PreviewResult = {
  entity: "employee",
  columns: ["employee_id", "first_name", "department", "email", "biography"],
  sources: {
    employee_id: "sequence",
    first_name: "faker",
    department: "weighted",
    email: "template",
    biography: "llm",
  },
  records: [
    {
      employee_id: "EMP-000001",
      first_name: "Tyrone",
      department: "Engineering",
      email: "tyrone@example.com",
      biography: "A long biography.",
    },
    {
      employee_id: "EMP-000002",
      first_name: "Shawn",
      department: "Sales",
      email: "shawn@example.com",
      biography: null,
    },
  ],
};

export function makeRun(overrides: Partial<RunView> = {}): RunView {
  return {
    id: "4f2a91c3-0000-0000-0000-000000000000",
    project_id: 1,
    revision_id: 3,
    state: "completed",
    seed: 42069,
    output_dir: "out",
    output_format: "jsonl",
    records_requested: 11200,
    records_written: 11200,
    progress: 1,
    duration_seconds: 12.5,
    config: {},
    estimate: {},
    summary: { files: ["out/employee.jsonl"] },
    error: null,
    created_at: "2026-08-15T10:00:00+00:00",
    started_at: "2026-08-15T10:00:00+00:00",
    finished_at: "2026-08-15T10:00:12+00:00",
    jobs: [
      {
        id: 1,
        run_id: "4f2a91c3",
        sequence: 0,
        type: "entity_batch",
        entity: "employee",
        state: "completed",
        offset: 0,
        requested: 5000,
        completed: 5000,
        remaining: 0,
        progress: 1,
        attempts: 1,
        part: 0,
        depends_on: [],
        outputs: ["out/employee.jsonl"],
        checkpoint: { completed: 5000 },
        error: null,
        started_at: null,
        finished_at: null,
        checkpointed_at: null,
      },
    ],
    statistics: [
      { scope: "quality", name: "constraint_validity", value: 1, detail: {} },
      { scope: "run", name: "records_written", value: 11200, detail: {} },
    ],
    ...overrides,
  };
}
