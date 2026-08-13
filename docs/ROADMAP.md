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

## Phase 2 — Providers

*Design document sections 10–13, 66, 76, 85.*

Makes `generator: llm` real.

- `LanguageModelProvider` adapters: Ollama, llama.cpp, OpenAI-compatible
- The Prompt Compiler (section 12) — turns semantic annotations, types,
  constraints and dependencies into provider-specific prompts, so users rarely
  engineer prompts by hand
- Structured output enforcement (section 13): extract, parse, validate, repair,
  retry, accept
- Generation modes (section 11): per-field, per-record, batch, contextual
  expansion
- Bounded retry with repair prompts (section 66)
- Content-addressed cache (section 76)
- `cacophony providers --test`, model listing, health checks

## Phase 3 — Runs

*Design document sections 29–32, 36, 55, 56, 64, 86.*

Makes generation durable and observable.

- Job system with states, checkpoints and resume (sections 29, 32)
- Per-provider concurrency and backpressure (section 30)
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

## Interpretations recorded during Phase 1

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

**Unimplemented generators (section 111).** `llm`, `image`, `tts`, `reference`
and `script` are registered and compilable now, so a forward-looking schema
validates, lints, plans and estimates correctly. At generation time they follow
section 65's failure-policy list: error by default, or emit a marked
placeholder, or emit null.

**Field `context` (section 49).** The field editor's Context list mixes related
entities and sibling fields, so each name is resolved against both, and a name
that is neither is reported as a typo.

**`script` generator (section 8).** Registered but unimplemented, and
deliberately so. Section 8 says scripts should run in an isolated environment
"where practical". A project file is something people share; an unsandboxed
`script:` field would make opening one equivalent to running a stranger's code.
The `expression` generator covers derived values safely today.
