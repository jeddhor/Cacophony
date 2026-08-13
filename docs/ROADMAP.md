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

## Phase 3 — Runs

*Design document sections 29–32, 36, 55, 56, 64, 86.*

Makes generation durable and observable.

- Job system with states, checkpoints and resume (sections 29, 32)
- Backpressure and cross-provider scheduling (section 30; per-provider limits
  landed with Phase 2)
- Metadata database (section 42)
- REST API and WebSocket progress feed (section 36)
- Structured logging and metrics (section 86)
- Resource controls (section 64)
- Run inspector (section 56)

## Phase 4 — Studio

*Design document sections 45–56.*

The React/TypeScript front end: project dashboard, schema studio, field editor,
distribution preview, relationship graph, generate screen, live run
visualisation.

## Phase 5 — Relational

*Design document section 91.*

Makes `generator: reference` real: foreign-key generation against materialised
key pools, the entity graph UI, stateful record context, SQLite and database
outputs, statistical distribution validation, the AI schema assistant
(section 50).

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

**Unimplemented generators (section 111).** `image`, `tts`, `reference` and
`script` are registered and compilable now, so a forward-looking schema
validates, lints, plans and estimates correctly. At generation time they follow
section 65's failure-policy list: error by default, or emit a marked
placeholder, or emit null. `llm` followed the same pattern until Phase 2
implemented it; nothing about the schemas written against it had to change.

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

**Field `context` (section 49).** The field editor's Context list mixes related
entities and sibling fields, so each name is resolved against both, and a name
that is neither is reported as a typo.

**`script` generator (section 8).** Registered but unimplemented, and
deliberately so. Section 8 says scripts should run in an isolated environment
"where practical". A project file is something people share; an unsandboxed
`script:` field would make opening one equivalent to running a stranger's code.
The `expression` generator covers derived values safely today.
