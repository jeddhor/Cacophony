# Cacophony

**A synthetic reality compiler.**

Cacophony generates enormous quantities of realistic, structured, internally
consistent synthetic data from schemas that describe what data *means* rather
than how to fabricate it.

You describe the shape and the meaning. Cacophony decides how to generate it.

The full design is in [CACOPHONY.md](CACOPHONY.md). This README covers what is
built and how to run it.

---

## Status: Phase 2 — Providers

Phase 1 delivered the deterministic core. Phase 2 makes `generator: llm` real:
local models write the semantic fields, and everything that comes back is
parsed, validated, repaired and retried before it is allowed near a dataset.

**Working now**

- Project schemas in YAML or JSON, with entities, fields, semantic annotations,
  types, constraints, chaos settings and output profiles
- Schema compiler: generator resolution, dependency graph, cycle detection,
  entity ordering, generation plan, workload estimates
- Schema linter with the checks from design document section 102
- Generator recommendation engine — a field with only a semantic description
  still gets a sensible generator instead of an expensive language-model call
- 25 registered generators: constant, sequence, uuid, random, boolean,
  distribution, weighted, lookup, pattern, template, expression, datetime, ip,
  mac, phone, government_id, faker, composite, transform, null and llm, plus
  the pending image/tts/reference/script declarations
- Hierarchical deterministic seeds — record *n* is identical whether generated
  first, last, in parallel, or after a resume
- Structural and constraint validation with repair
- Streaming exporters: CSV, JSON, JSON Lines, Parquet
- Language-model generation against Ollama, llama.cpp and any
  OpenAI-compatible server, addressed by URI
- The Prompt Compiler — you write what a field *means*, it writes the
  instruction and the JSON Schema that constrains the answer
- Structured output enforcement: extract, parse, validate, repair, retry
- Four generation modes: per-field, per-record, batch, contextual expansion
- Content-addressed cache, so a re-run costs nothing it has already paid for
- Credentials resolved from the environment or the OS keychain, never from the
  project file
- CLI: `validate`, `lint`, `plan`, `prompt`, `preview`, `generate`,
  `generators`, `providers --test`, `models`

**Declared but not yet implemented** — the interfaces exist so later phases
extend the platform rather than rewrite it, as design document section 111
requires. Fields using these compile, lint, plan and estimate correctly:

- `image`, `tts` — need the multimodal phase
- `reference` — needs the relational phase
- `script` — needs sandboxed execution

Set `on_unavailable: placeholder` on any of them to run the whole pipeline
today with obviously-marked stand-in values.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the phase plan.

---

## Install

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,parquet]"
```

## Try it

```bash
# Compile the schema and report problems
cacophony validate templates/corporate-directory.yaml

# See what the compiler decided, and why
cacophony plan templates/corporate-directory.yaml

# Warn about questionable designs
cacophony lint templates/corporate-directory.yaml

# Sample 10 records, with each column's generation source labelled
cacophony preview templates/corporate-directory.yaml --entity employee -n 10

# Generate for real
cacophony generate templates/corporate-directory.yaml \
  --records 100000 \
  --seed 42069 \
  --output parquet \
  --out-dir out/
```

### Language-model fields

`templates/conversational-ai.yaml` points at the built-in `mock` adapter, an
in-process model that answers against the schema the prompt compiler produces.
It runs with no server, so the whole path is visible immediately:

```bash
# See the prompt Cacophony wrote on your behalf
cacophony prompt templates/conversational-ai.yaml --schema

# Generate, with a cache so the second run is free
cacophony generate templates/conversational-ai.yaml -n 200 \
  --cache read_write --cache-path .cacophony/cache.db
```

Point it at a real model by changing three lines:

```yaml
providers:
  assistant:
    adapter: ollama            # or llamacpp, or openai_compatible
    base_url: http://localhost:11434
    model: llama3.1:8b
```

Then check it is reachable:

```bash
cacophony providers templates/conversational-ai.yaml --test
cacophony models templates/conversational-ai.yaml
```

Nothing else in the schema changes.

## A schema

```yaml
project:
  name: Example Corporate Dataset
  seed: 42

entities:
  employee:
    count: 10000
    fields:
      employee_id:
        type: string
        primary_key: true
        generator: sequence
        format: "EMP-{000000}"

      first_name:
        type: string
        semantic: "Person's given name"

      last_name:
        type: string
        semantic: "Person's family name"

      email:
        type: email
        generator: template
        template: "{first_name|lower}.{last_name|lower}@example.com"

      department:
        type: enum
        generator: weighted
        choices:
          Engineering: 40
          Sales: 25
          Finance: 15
          Operations: 20

      biography:
        type: text
        semantic: >
          A short fictional professional biography consistent with the
          employee's department and start date.
        generator: llm
        context: [department, first_name, last_name]
        constraints:
          max_length: 400
```

`first_name` and `last_name` name no generator. The recommendation engine
routes them to Faker — not to a language model, which at ten thousand records
would cost hours for values Faker produces in microseconds.

`email` depends on both, so the compiler orders it after them automatically.

`biography` names no prompt. The prompt compiler writes one from the meaning,
the type, the length constraint and the declared context — and because the
deterministic fields are generated first, the model is asked to *enrich* a
record that already has a name and a department rather than invent one from
nothing.

---

## Repository layout

Follows design document section 96.

```
backend/cacophony/
├── core/          types, seeds, records, contexts, the four key interfaces
├── schema/        project model, compiler, dependency graph, plan, linter
├── generation/    generator registry, built-in generators, recommendation, engine
├── validation/    structural and constraint validators, the record pipeline
├── outputs/       CSV, JSON, JSONL and Parquet writers
├── providers/     language-model adapters, cache, secrets, provider registry
├── scenarios/     scenario engine (later phase)
├── plugins/       plugin protocol (later phase)
├── api/           REST API (later phase)
└── cli/           command-line interface
templates/         starter project schemas
examples/          worked examples
tests/             unit and integration tests
docs/              architecture and reference documentation
```

Two deliberate deviations from section 96, both noted in the relevant module
docstrings: `cli/` lives inside the backend package because it is a thin layer
over the same objects the API will use, and `generation/planner/` holds
run-time planning strategies while the schema-level planner lives in
`schema/compiler.py` — a plan is a compiled artifact of the schema.

## Development

```bash
pytest                  # tests
ruff check .            # lint
ruff format .           # format
mypy                    # type check
```

## Design notes worth knowing

**Seeds are hashes, not streams.** A record's seed is derived by hashing its
position in the project → entity → record → field hierarchy. Nothing depends on
generation order, so parallel execution, resumed runs and repeated previews all
produce identical values (design document section 75).

**Identifiers are safe by default.** Generated emails, domains, IPs and MAC
addresses come from ranges the standards bodies reserved for documentation, so
a synthetic log can never point at a real host (section 62). Set `safe: false`
where realistic-valid values are genuinely needed.

**Everything streams.** The engine yields bounded batches and writers flush
them, so a dataset far larger than RAM costs the same memory as a small one
(section 31).

**The model is never trusted.** Output is extracted from whatever wrapper it
arrived in, parsed, type-checked, constraint-checked and repaired where repair
needs no judgement. What cannot be repaired is retried with a prompt quoting
what was wrong, then with the schema restated. The ladder is three model calls
long and then stops (sections 13 and 66).

**Prompts are compiled, not written.** A field's meaning, type, constraints,
dependencies, tone and examples are all already in the schema, so the prompt
compiler assembles them (section 12). `cacophony prompt` shows you exactly what
was assembled.
