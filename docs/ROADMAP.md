# Cacophony development roadmap

Maps the phases of this repository onto [CACOPHONY.md](../CACOPHONY.md).

The design document defines its own phases in sections 90–95 and a first
milestone in section 111. Those describe *capability* tiers. The phases below
are *delivery* slices: each one ends with a working build, a passing test suite
and a commit, and each is reviewed before the next begins.

---

## Phase 1 — Foundation ✅

**Delivered.** Design document sections 4, 7, 8, 9, 57, 62, 68, 75, 96–103.

The deterministic core, end to end. A user can describe an unfamiliar data
structure without writing code and obtain a large, structurally valid,
internally consistent, reproducible dataset in four formats.

- Project schema: entities, fields, semantic annotations, 29 primitive types,
  constraints, relationships, providers, scenarios, chaos settings, output
  profiles
- Schema compiler: generator resolution, dependency graph, cycle detection,
  entity ordering, generation plan, workload estimation
- Schema linter (section 102)
- Generator recommendation engine (section 68)
- 25 registered generators, 20 of them fully implemented
- Hierarchical deterministic seeds (section 75)
- Structural and constraint validation with repair (sections 13, 57)
- Streaming CSV, JSON, JSON Lines and Parquet writers (sections 31, 33)
- CLI: `validate`, `lint`, `plan`, `preview`, `generate`, `generators`,
  `providers`
- Provider, scenario and plugin interfaces, empty but real (section 111)

**Not in this phase, deliberately:** anything requiring a model server, a
materialised key pool, or durable run state. Those have their own phases below,
and shipping half of one would have meant shipping data that looks right and
means nothing.

---

## Phase 2 — Providers ✅

**Delivered.** Design document sections 10–13, 30, 43, 61, 63, 66, 76, 85.

`generator: llm` is real. A field that says what it *means* is written by a
local model, and nothing the model returns reaches a dataset unexamined.

- Adapters: `ollama`, `llamacpp`, `openai_compatible`, plus an in-process
  `mock` for tests and rehearsals. All addressed by URI; Cacophony owns no
  models (section 85)
- The Prompt Compiler (section 12): semantic annotations, types, constraints,
  dependencies, examples, forbidden values, tone, locale and entity context
  become a prompt and a JSON Schema. `cacophony prompt` shows the result
- Structured output enforcement (section 13): extraction from fenced blocks,
  preambles and truncated responses; parsing; type and constraint validation;
  deterministic repair; retry
- The retry ladder of section 66, exactly: normal, repair prompt, explicit
  schema, then stop. Three model calls and no more
- Generation modes (section 11): per-field, per-record, batch and contextual
  expansion. Deterministic fields are always produced first, so the model
  enriches a record rather than inventing one
- Content-addressed cache over provider, model, prompt, settings and seed,
  with `disabled` / `read_only` / `read_write` modes (section 76)
- Per-provider concurrency limits (section 30) and a circuit breaker, so a
  downed server costs one call rather than one per record
- Secrets resolved from the environment or the OS keychain; the loader rejects
  a credential written into a project file (section 63)
- Prompts instruct against real identities and domains (section 61)
- CLI: `prompt`, `providers --test`, `models`, `--cache`, `--llm-batch-size`

**Structural change in this phase:** the engine now builds a chunk of records
in lockstep, layer by layer, instead of one record at a time. That is what
allows one call to cover several fields of several records. Deterministic
generation is unaffected — same order, same seeds, same output.

---

## Phase 3 — Runs ✅

**Delivered.** Design document sections 6, 28–32, 36, 42, 51, 55, 56, 64, 73,
86, 87, 108.

Generation is now durable. A run is a set of jobs; it checkpoints as it goes,
records the exact schema revision it used, and can be paused, cancelled and
resumed from a terminal or over HTTP.

- The **Conductor** (section 108): plans a run into jobs, executes independent
  entities concurrently within `--workers`, and honours pause/resume/cancel at
  batch boundaries — never mid-record
- Job states and types exactly as section 29 lists them, with declared
  transitions rather than implied ones
- Checkpointing (section 32) after *every* batch, plus reconciliation against
  the file on disk before appending, so an interrupted run resumes without
  duplicating or skipping a record
- Formats with a footer — JSON arrays, Parquet — resume into a new part file
  rather than corrupting the one they were writing
- Metadata store in SQLite (section 42): projects, schema revisions, runs,
  jobs, events, statistics. Generated datasets are deliberately not in it
- Schema revisions (section 73): a run records the schema it used, verbatim,
  and resumes under that one
- REST API (section 36) and a WebSocket feed of live progress (section 55)
- Structured logging with section 86's fields, in text or JSON; debug payloads
  withheld unless asked for (section 87)
- Resource controls (section 64), including a disk check before the first
  record rather than after the last
- Run inspector (section 56) and quality metrics (section 58)
- CLI: `generate` now records a run, plus `runs`, `run`, `resume`, `serve`

**Deliberately not shredded into tables.** Section 42 lists `entities`,
`fields`, `relationships`, `generator_configs` and `scenarios` as tables. They
are stored instead as the schema text they came from, because section 74 wants
that schema reviewed in Git and two sources of truth would immediately drift.
Section 73's requirement — that a run record the exact revision it used — is
what the store actually needs, and a verbatim revision satisfies it exactly.

---

## Phase 4 — Studio ✅

**Delivered.** Design document sections 40, 45–56, 74.

Cacophony Studio: React, TypeScript, Vite, TanStack Query, Zustand and React
Flow — section 40's stack, for section 45's "controlled chaos".

- **Visual identity** (section 45): dark graphite, luminous violet, electric
  cyan, magenta, translucent panels, a converging-waveform motif. Colour is
  never the only signal — every state carries a word as well as a hue
- **Navigation** (section 46): Projects, Studio, Generate, Runs, Providers,
  Assets, Plugins, Settings. The two later-phase destinations are shown
  disabled rather than hidden, because the shape of the product is part of what
  the interface communicates
- **Project dashboard** (section 47): entities, output, relationships, media,
  the workload estimate and the linter's findings, then the two doors
- **Schema Studio** (section 48): entities on the left, fields in the centre,
  generation properties on the right, plus preview, graph and source tabs
- **Field editor** (section 49): name, type, meaning, generation, context,
  constraints, tone, null probability, and a button that samples records
- **Data preview** (section 51): a generation-source row under the header, and
  full provenance on any cell
- **Distribution preview** (section 52): weighted choices drawn as bars
- **Relationship graph** (section 53): React Flow, laid out in the dependency
  layers the compiler already computed. Cyan edges are derived dependencies —
  the reason entities generate in the order they do — and violet ones are
  declared relationships
- **Generate screen** (section 54): scale, workload, providers, disk estimate,
  the plan and the warnings, then START CACOPHONY. The estimate rescales live
  as the record count and entity selection change
- **Live run view** (section 55): per-entity counters, records per second,
  tokens per second, ETA, all fed by the WebSocket, with pause, resume and
  cancel
- **Run inspector** (section 56) and quality metrics (section 58)

**Schema editing that does not destroy the schema.** Section 48 wants a GUI;
section 74 wants YAML a team reviews in Git. A form that saved by
re-serialising its own model would satisfy the first and ruin the second. So
edits are sent as *targeted operations* and applied to the document with
ruamel's round-trip parser: changing a count changes one scalar, and the
comment above it survives. The whole patch is verified before anything is
written, so a rejected edit leaves the file byte-for-byte as it was.

## Phase 5 — Relational ✅

**Delivered.** Design document sections 8, 15, 26, 33, 50, 57, 58, 91.

`generator: reference` is real. Entities point at one another, the keys
resolve, and a field that reads a parent's other columns reads the parent its
own row chose.

- **References computed, not looked up** (sections 15, 75). A record's seed is
  derived by hashing its *position*, so parent 4,823,913 can be reconstructed
  directly. A foreign key is therefore arithmetic: pick an index in the
  parent's range, generate exactly the fields that produce that parent's key.
  No key pool, no memory proportional to the parent count, and the same answer
  whichever order the entities were generated in
- **Reference distributions** (section 15): `uniform`, `skewed`, `sequential`
  and `round_robin`. `skew` is documented as an exact figure — the busiest
  tenth of parents take `0.1 ** (1 / skew)` of the references — so a schema can
  choose a shape rather than hope for one
- **Cross-entity field access**: `{company.domain}` in a template, or
  `customer.country` in an expression, resolves against the record *this row*
  referenced. The compiler works out that such a field must be generated after
  whichever field chose the parent; nobody writes that dependency down
- **Types that match across the join**: a reference with no declared type takes
  the type of the key it points at, and a field that named a generator but no
  type takes the type that generator produces. An integer key referenced by a
  string column joins to nothing
- **Referential and statistical validation** (section 57): sampled foreign-key
  checks, and total-variation distance between declared and generated
  distributions. Both feed section 58's quality report, in the CLI, the API and
  the Studio
- **SQLite and SQL script outputs** (section 33): one database for the whole
  project, with `FOREIGN KEY … REFERENCES` clauses the database enforces and
  column types taken from the schema. `PRAGMA foreign_key_check` is silent on
  what Cacophony produces
- **Reference linting**: unique references that outnumber their parents,
  references to empty entities, references to non-unique keys, self-references,
  and evenly spread references at a scale where evenness is implausible
- **The AI schema assistant** (section 50): a description becomes a compiled,
  linted schema. `cacophony propose "employees, laptops and login activity"`
- **Studio**: the relationship graph draws foreign keys labelled with the field
  that carries them, and the run view shows generated distributions against
  declared ones
- `templates/retail-commerce.yaml`: customer → order → order_item → product,
  the four-entity shape with a child that has two parents

**The division of labour in the assistant.** The model proposes structure —
what entities exist, what fields they have, what each field *means*, which
entity points at which. Cacophony chooses the generators, because that is a
question about Cacophony and the recommendation engine (section 68) answers it
better than a model can. Every proposal is compiled and linted before it is
shown; one that does not compile is handed back to the model with the error
attached, and if the second attempt fails too, nothing is returned.

## Phase 6 — Multimodal

*Design document section 92.*

Makes `generator: image` and `generator: tts` real: InvokeAI integration, TTS
integration, document templates and PDF generation, the asset manager, artifact
pipelines, media metadata.

## Phase 7 — Worlds

*Design document section 93.*

Persistent world state, the scenario engine, temporal simulation, stateful
simulation, correlated event streams, entity histories, organisation
generators.

## Phase 8 — Live and distributed

*Design document sections 94, 95.*

Continuous generation to syslog, HTTP and Kafka; the streaming dashboard;
remote workers, capability discovery, job leasing, shared artifact storage.

---

## Interpretations recorded during development

Where the design document left room, these are the readings taken and why.

**Repository layout (section 96).** `cli/` lives inside the backend package
rather than beside it: the CLI is a thin layer over the same schema, generation
and output objects the API will use, and a separate distribution would buy only
an import path. `generation/planner/` holds run-time planning strategies while
the schema-level planner lives in `schema/compiler.py`, because a plan is a
compiled artifact of the schema.

**Async generators (section 97).** The `Generator` interface is asynchronous as
specified, and `SyncGenerator` implements it while also exposing a synchronous
path the engine can call directly. Deterministic generators do not need an
event loop, and section 89's throughput target leaves no room to pay for one
per field per record.

**Sampling isolation (section 103).** Requires that previewing not alter
production output. Because seeds are derived by hashing a record's *position*
rather than by advancing a shared stream, sampling cannot consume randomness a
run would have used — the requirement is met structurally. Preview therefore
shows exactly the records a real run will produce, which is the more useful
behaviour; `--isolate` is available when a different sample is wanted.

**Seed derivation (section 75).** Two routines rather than one. `derive_seed`
hashes with BLAKE2b and handles arbitrary labels. `mix_seed` is an integer
mixer used for the per-record and per-field levels, where the derivation runs
once per generated value. Both are deterministic, order-independent and well
distributed; neither is cryptographic, and nothing depends on their being so.

**Unimplemented generators (section 111).** `image`, `tts` and
`script` are registered and compilable now, so a forward-looking schema
validates, lints, plans and estimates correctly. At generation time they follow
section 65's failure-policy list: error by default, or emit a marked
placeholder, or emit null. `llm` followed the same pattern until Phase 2
implemented it and `reference` until Phase 5; nothing about the schemas written
against either had to change.

**Generation modes (section 11).** `expansion` is a synonym for `per_record`
rather than a separate code path, because contextual expansion is what every
mode already does: the engine produces deterministic fields first and asks the
model to enrich them. Section 11 calls that "often the optimal strategy", so it
is the default rather than an option.

**Structured output (section 13).** Repair is limited to fixes that need no
judgement — unwrapping a fenced block, closing a truncated string, trimming to
a declared maximum, clamping a number into range. Anything requiring a decision
becomes a retry, because a repair that guesses at meaning is indistinguishable
from fabricated data.

**Batch shortfalls.** A model asked for ten records that returns nine has given
a wrong answer to a well-formed question, so it goes back up the retry ladder
like any other. Records that did arrive are kept; only the remainder falls
through to the field's failure policy.

**Checkpoint granularity (section 32).** One job per entity, not per chunk.
Section 32's own example is per entity, and a single integer is a complete
checkpoint here because seeds are derived from record indices rather than from
RNG state — there is nothing else to restore.

**Checkpoint frequency.** Progress is written after every batch, not every
`--checkpoint-every` records; that option controls how often a checkpoint is
*announced* to the log and the live feed. A checkpoint that lagged the file
would make a resume duplicate whatever fell in the gap, and one small UPDATE
per batch is far cheaper than being wrong.

**Job types (section 29).** `llm_batch`, `image` and `audio` are declared but
are not separate jobs yet; that work happens inside the entity job that needs
it. Scheduling a record and the biography that belongs to it as independent
units would add a round trip for no benefit until there are separate machines
to schedule them onto (section 95).

**Alembic (section 39).** Recommended, and not yet used. Alembic earns its keep
when there are deployed databases to migrate *from*; every store today was
written by a pre-release build. There is a version stamp and an explicit
upgrade ladder, checked on every open, so the day a real migration is needed
the store says so rather than failing obscurely.

**Runs execute in the API process.** No broker, no worker pool. Section 39 is
explicit that Redis and Celery should be avoided until distributed execution is
actually required (section 95), and generation yields to the event loop between
batches, so the API stays responsive during a run.

**The Studio is served by the backend.** `npm run build` emits into
`backend/cacophony/api/static/`, and `cacophony serve` mounts it — one process,
one origin, no CORS. The mount is added after every API route and serves
`index.html` as a fallback, so the client router owns its own URLs and a
reloaded `/runs/abc123` still gets the application.

**A form cannot edit everything.** The field editor handles scalar options;
choices, histogram bins and lists are shown read-only there and edited in the
source tab, which replaces the document wholesale. Pretending a form could
round-trip arbitrary YAML would mean either a far larger editor or a lossy one.

**No component library.** The interface is hand-written CSS over custom
properties. Section 45 asks for a specific visual identity rather than a
generic admin panel, and adopting a design system would have meant fighting its
defaults to get there.

**No drag-and-drop field reordering yet.** Section 48 mentions dragging fields.
The `move_field` operation exists and is tested; wiring a drag interaction to
it is a small addition that did not earn its place ahead of the editing,
preview and run views.

**Field `context` (section 49).** The field editor's Context list mixes related
entities and sibling fields, so each name is resolved against both, and a name
that is neither is reported as a typo.

**`script` generator (section 8).** Registered but unimplemented, and
deliberately so. Section 8 says scripts should run in an isolated environment
"where practical". A project file is something people share; an unsandboxed
`script:` field would make opening one equivalent to running a stranger's code.
The `expression` generator covers derived values safely today.

**References at scale (sections 15, 26).** Section 15 asks that computers
belong to employees and login events reference those computers. The obvious
implementation is a materialised key pool: generate the parents, keep their
keys, hand them out. That is correct and it does not scale — ten million
parents is a data structure larger than most of the datasets Cacophony is asked
to produce, held for the duration of a run that produces something else.

Because seeds are derived from position rather than from stream order
(section 75), the pool is unnecessary: any parent can be reconstructed on
demand from its index, and only the fields its key depends on need generating.
Two bounded caches sit on top — recent keys, and recent whole parent records
for the fields that want more than the key — and both are pure accelerators. A
cold cache changes speed and nothing else, which is asserted in the tests
rather than assumed.

One consequence worth stating: resolving a parent never makes a model call.
Provider-backed fields are skipped when a parent record is derived, because one
login event should not cost a biography.

**Sampled referential checks (section 57).** Verifying ten million foreign keys
one at a time costs more than generating them did. Referential validation
therefore checks a bounded sample and reports how large a sample it was, which
is an honest partial answer rather than an expensive complete one nobody waits
for. The check earns its place despite references being valid by construction,
because chaos injection deliberately breaks them (section 78) and because a
schema can point at an entity a particular run did not generate.

**Reference distribution defaults.** `uniform` is the default because it is the
one that cannot surprise anyone. It is also wrong for almost every real
dataset, which is why `skewed` exists and why the linter mentions it when a
project spreads half a million children evenly over a few hundred parents. The
default `skew` of 1.6 is deliberately moderate: over-skewing by default would
put most of a dataset on a handful of parents and leave the rest barely
exercised, and too-concentrated data looks real until someone queries the tail,
while too-even data is obvious immediately.
