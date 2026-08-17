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

## Phase 6 — Multimodal ✅

**Delivered.** Design document sections 18–23, 81, 82, 92.

`generator: image`, `generator: tts` and `generator: document` are real. One
record produces several artifacts, each derived from that record's own values,
and every file is accounted for.

- **The asset store** (sections 19, 81): paths derived from entity, record
  index and field, so the same record always writes the same file and a
  resumed run skips what it already produced. Identical bytes are stored once,
  hard-linked where the filesystem allows. A `manifest.jsonl` sidecar answers
  "what belongs to E48291?" without scanning the dataset
- **Image providers** (section 18): `invokeai`, which submits a workflow graph
  — a default text-to-image graph, or one the project supplies — and polls the
  queue with a deadline; and `procedural_image`, which draws deterministic
  identicons, portraits, product cards and document thumbnails in-process
- **Speech providers** (section 20): `piper` and `openai_speech`, covering the
  two shapes local TTS servers expose; and `procedural_speech`, which
  synthesises voice-shaped audio with a per-voice pitch
- **Document rendering** (section 23): placeholder-filled templates to PDF,
  HTML or text. The PDF writer is part of Cacophony rather than a dependency
- **Media metadata** (section 19): provider, workflow, seed and prompt hash on
  every image; voice, duration and an aligned transcript on every audio clip
- **`templates/multimodal-support.yaml`** (section 82): employees with
  portraits and ID badges, calls with recordings and transcripts. It runs with
  no GPU and no model server
- **The asset browser**: section 46's Assets destination, enabled. Images
  shown, audio playable, documents linked, provenance on every card
- **API**: `GET /api/runs/{id}/assets` and a file route that refuses any path
  outside the run's own directory
- CLI: `--assets-dir`, `--regenerate-assets`, and an asset line in the summary

**Real formats, written here.** PNG, WAV and PDF are all produced from the
standard library. Taking Pillow and a PDF library as dependencies to avoid a
few hundred lines would put wheels with native code in front of every user who
generates CSV and never touches an image. The outputs are verified against
`file`, `pdftotext` and `qpdf --check` rather than against Cacophony's own
readers.

**Verified against real servers.** The image and speech adapters were first
written from the published APIs and then run against InvokeAI 6.13.8 and
piper1-gpl. Both had defects that only a real server could show, and both were
of the same kind — the documentation described the shape, and the shape was not
the whole contract:

- **InvokeAI takes a model *identifier*, not a name.** A node wants
  `{key, hash, name, base, type}`, which only the server can supply. The
  adapter now reads the model list once and resolves whatever the schema
  called the model into that structure.
- **Architectures wire differently.** SDXL has two text encoders; FLUX and
  Qwen-Image have entirely different topologies. There are now two built-in
  graphs and a clear refusal for the rest.
- **Piper serves WAV under `text/html`.** Flask's default for a bare byte
  response. Believing the header would have filed every recording as `.bin`,
  so the payload is sniffed and the header trusted only when plausible.
- **InvokeAI's seed field is 32-bit.** A Cacophony seed is 64-bit and was
  rejected outright.

The lesson is recorded in `tests/test_provider_contracts.py`: offline tests run
against responses *captured* from the real servers rather than invented, since
an invented fixture agrees with whatever the adapter already believes. Live
tests run when `CACOPHONY_TEST_INVOKEAI`, `CACOPHONY_TEST_PIPER` or
`CACOPHONY_TEST_OLLAMA` name a server, and are skipped otherwise — a suite that
needs a GPU is a suite nobody runs.

**Procedural providers are not a stand-in for a stand-in.** An image field
changes the shape of a project — records grow assets, paths appear in the
output, the summary reports files and bytes — and all of that deserves to be
exercisable by someone with no GPU, in CI, in a test suite. The procedural
providers produce deterministic, obviously-synthetic media and label every
result `synthetic: true`. Point the same schema at InvokeAI or Piper by
changing one adapter line.

## Phase 7 — Worlds ✅

**Delivered.** Design document sections 16, 17, 24, 25, 26, 71, 78, 93.

Records stop being independent rows and become a *history*: each identity's
events belong to it, arrive in order, follow the working week, carry a running
balance, and a fraction of those identities are having a bad month.

- **Temporal simulation** (section 25): a project declares a period and a
  shape — weekday and hour curves, seasonality, holidays, promotional spikes,
  growth — compiled into a cumulative distribution. `business_hours`, `retail`,
  `evening` and `flat` ship as named shapes
- **Event allocation**: events are laid out in contiguous blocks per subject
  rather than each choosing a parent at random, so "the fortieth login of this
  employee" is a question a record can answer in O(log P)
- **Stateful simulation** (section 26): balances, counters and statuses folded
  over a subject's own events, with `min`, `max` and `precision`
- **The scenario engine** (section 17): a scenario selects *subjects*
  deterministically, occupies a window, runs in ordered phases, and applies
  effects — constants, weighted choices or expressions — after normal
  generation
- **Entropy injection** (sections 24, 78): the `chaos:` block, which has parsed
  and done nothing since Phase 1, now damages records. Five presets from
  `pristine` to `absolute`; seven defect kinds; every defect recorded
- **Persistent worlds** (section 16): `cacophony worlds project.yaml --create
  acme`, then `--world acme` on any later run
- **`templates/security-operations.yaml`** rewritten: section 71's scenarios
  execute rather than merely being declared

**The architectural tension, and how it was resolved.** Everything before this
phase rests on record *n* being a pure function of *n* — which is what makes
runs parallel, resumable and order-free. A running balance is the opposite: it
depends on what came before.

The resolution is that **state is a fold over a partition, not over the
dataset**. A balance belongs to an account; the allocation lays each subject's
events out contiguously, so event *k*'s state depends only on events *0..k of
that subject*. Parallelism is preserved across subjects, which is where it
actually lives — there are thousands of accounts and only one dataset.

Resume needs no serialised state. To restart at event 4,823,913 the machine
replays that subject's block, which is bounded by how many events one account
has, and continues. Verified: resuming mid-block replays 2–61 earlier events,
not 1,777, and produces balances identical to an uninterrupted run.

**Timestamps are ordered without being sorted.** The timeline compiles its
shape into a cumulative distribution and produces moments by inverse transform
at a quantile. Drawing at quantile *k/n* for the *k*-th of a subject's *n*
events therefore yields a run of timestamps already in chronological order — no
sort, no memory, and the same answer whichever record is generated first.

A second property falls out of that, and the scenario engine depends on it: a
fraction of a subject's *history* is the same calendar moment as that fraction
of the *timeline*. So an incident declared at `window: {at: 0.62}` lands in the
same week for an identity with sixty sign-ins and one with fifteen hundred,
which is what makes it a correlated incident rather than a per-user
coincidence.

## Phase 8 — Live ✅

**Delivered.** Design document sections 35, 94.

Cacophony becomes a workload generator: a rate per entity, one or more
destinations, and a stream that runs until you stop it.

```bash
cacophony stream templates/security-operations.yaml \
    --rate authentication=250/s --rate security_finding=8/minute \
    --to syslog://siem.internal:514
```

- **Rates as people write them**: `250/s`, `8 per minute`, `1200/hour`
- **Destinations** (section 35): `stdout`, a rotating `file`, `syslog` over UDP
  or TCP in RFC 5424 or 3164, `http` posting ndjson batches, and `kafka` behind
  an optional dependency
- **A streaming dashboard**: achieved rate against requested, per entity, per
  destination, with failures
- **Adjustable rates**: `retarget` changes an entity's rate mid-flight, and the
  accrued backlog is trimmed so slowing down takes effect at once
- **Long-running**: `--seconds`, `--records`, or until interrupted; Ctrl-C
  finishes the current batch, closes the destinations and reports the totals
- **Resumable**: `--from N` continues the index sequence rather than replaying
  it, and the summary prints the number to use

**What a stream changes, and what it does not.** Three things genuinely differ
from a batch run, and being clear about them is most of the design.

*There is no total.* Indices simply keep going, which costs nothing because a
record's seed is derived from its index (section 75) — event 4,823,913 of a
stream is the same record it would have been in a batch run of the same schema.

*Time is now.* A batch dataset covers a period that has happened; a stream
produces events that are happening, so timestamps come from the wall clock.
`--historical` keeps the generated ones. `--follow-shape` reuses the timeline's
*shape* as a rate multiplier instead, so the stream is quiet at three in the
morning.

*Subjects interleave.* A batch run lays each subject's events out in a
contiguous block, which is what makes ordered histories and folded state cheap.
A stream cannot — its events arrive mixed together, because that is what a
stream is — so the subject is drawn per event from the same weighted
distribution and the per-subject counter is kept in memory. The first version
of this shipped the batch layout by mistake and every record in the stream
belonged to subject zero.

Scenario windows are reinterpreted for the same reason: an incident declared at
`window: {at: 0.62}` has no meaning in an endless stream, so it *recurs* over
`--scenario-cycle` seconds, which is what a detection exercise wants anyway.

**Attainment is the number that matters.** A workload generator that reports
"12,000 records delivered" while quietly running at sixty per cent of the rate
it was given is measuring the wrong thing, so achieved-over-requested is
computed, displayed and warned about. Getting it to 99% took three fixes, each
of which had been costing several per cent silently: the tick slept a fixed
interval *plus* however long generation took; the per-entity batch ceiling
capped throughput once work time was significant; and the loop slept past its
own deadline, leaving the last partial batch unclaimed in the buckets.

Measured on the security-operations template: 99.1% at 50/s, 99.2% at 280/s
across two entities, 99.5% at 1,000/s. At 5,000/s attainment drops to 75% —
generation, not delivery, is the limit — and the dashboard says so rather than
reporting a number the operator did not ask for.

**A memory bound is not a throughput bound.** The first version took whatever
the token bucket owed and generated it in one chunk. Asked for five million
events a second it built a five-million-record list, and the machine ran out of
memory — the exact failure the module docstring claimed to prevent ("a sink
that cannot keep up slows the stream rather than filling memory"). One tick now
materialises at most `max_in_flight` records per entity whatever the rate says;
the surplus stays in the bucket, the stream runs at what it can actually do,
and attainment reports the gap. The same case now peaks at 127 MB and reports
0.4%.

**Closed after Phase 9.** The REST routes and the Studio streaming page landed
later, and are described under "Phase 8, completed" below.

---

## Phase 9 — Distributed ✅

**Delivered.** Design document sections 84, 95.

A run is cut into shards, handed out as leases, and reassembled. Workers
advertise what they can do; the scheduler gives them only shards they can do.

```bash
# One machine, several workers
cacophony cluster templates/security-operations.yaml -o out/ --workers 8

# Several machines
cacophony controller templates/security-operations.yaml --port 8787
cacophony worker templates/security-operations.yaml \
    -c http://controller:8787 -o /mnt/shared
```

- **Shards are index ranges** (`entity`, `offset`, `count`) that tile each
  entity exactly once
- **Capability routing** (section 84): a shard's requirements are read off its
  compiled generators, a worker's from its configured providers, and the
  scheduler hands over the first shard the worker can actually run
- **Leases with generation counters**: a worker renews while it works; a lease
  that expires is reassigned, and the original holder is told its lease is
  stale rather than allowed to double-write
- **Worker health**: last-seen, throughput, what each node is holding, and a
  `stalled` flag when the remaining work needs a capability nobody alive has
- **Shared artifact storage**: assets are content-addressed (section 81) so two
  nodes writing the same file agree; each node appends to its own manifest and
  a reader reads them all back as one run
- **Assembly**: parts are named after their offset, and `jsonl`, `csv` and
  `json` join back into one file; `parquet` stays a directory of parts, which
  every reader for that format accepts

**The claim the phase rests on.** A dataset produced by many workers is
*byte-identical* to the same dataset produced by one. This is not a goal that
was worked towards; it falls out of section 75. A record's seed is a hash of
its position, so record 4,823,913 is the same record on any machine, in any
order, at any time. There is no RNG state to partition, no ordering to restore
and no merge step.

Verified rather than asserted, at every level:

- 4 workers, shard size 137, against a single-machine reference: identical
- The same with the security-operations template — timelines, per-subject state
  folds, executing scenarios and deliberate chaos — identical
- 1 worker, 2 workers and 7 workers all produce the same bytes
- Two real worker *processes* over HTTP, against a controller in a third:
  identical
- A worker `kill -9`'d mid-shard, its lease reclaimed and reassigned: still
  identical, including the half-written file the dying process left behind

**Retry is regeneration.** The usual hard part of a lease protocol — recovering
partial work from a dead worker — costs nothing here, so it is not attempted. A
reassigned shard is redone from scratch, and because a shard is a pure function
of its index range the second attempt produces exactly the bytes the first one
would have. The replacement writes to the same offset-named path, so the dead
worker's truncated file is overwritten rather than left to corrupt the dataset.

**Two failure modes worth naming.** A worker holding a *different schema* is
refused at registration: two schemas make a dataset that is neither, and the
failure would otherwise surface as data nobody could explain. And a resent
`complete` — because the network lost an acknowledgement, not because anything
went wrong — is refused rather than counted twice; the HTTP tests found that
one, which the in-process tests had not.

**Local and distributed are one code path.** `cluster` runs a real controller
and real workers over `LocalTransport`; `controller`/`worker` run the same
objects with JSON in between. A lease protocol that only ever runs across
machines is a lease protocol nobody tests.

**Not in this phase:** the in-process workers are asyncio tasks, not processes,
so `cluster` overlaps waiting rather than multiplying cores. It is a real
speed-up for a run that calls a model, a disk or a socket, and roughly a wash
for one that is pure arithmetic. `generate` remains the single-node path with
run records, checkpoints and resume; the distributed commands trade that
bookkeeping for parallelism, which is honest because a shard needs no
checkpoint.

---

## Phase 8, completed — streams over HTTP ✅

**Delivered.** Design document sections 35, 36, 94. The one thing Phase 8 left
open: a stream you can start, watch and *steer* from somewhere other than a
terminal.

```
POST   /api/projects/{id}/streams      GET  /api/streams
GET    /api/streams/{id}               GET  /api/streams/{id}/records
POST   /api/streams/{id}/retarget      POST /api/streams/{id}/pause|resume|stop
DELETE /api/streams/{id}               WS   /api/streams/{id}/feed
```

- **A Studio streaming page**: rates as editable fields with presets, a
  retarget button per entity, per-destination delivery counts, the live
  configuration, and a table of the records going past
- **`retarget` is finally called by something.** It existed and was tested from
  Phase 8; nothing used it. Over HTTP it becomes what section 94 describes — a
  workload you turn up while watching what it does to whatever is receiving it
- **A `memory://` sink**: a bounded ring of recent records, because a browser
  cannot tail a file on the server or read its stdout. Bounded by a `deque`, so
  a stream at 50,000/s costs what one at 5/s costs
- **Streams are process state, not history.** They are held in a service rather
  than the run store: a stream that outlived the server producing it would be a
  row describing traffic nobody is sending. Shutdown stops them first, so a
  server going away stops sending somebody's collector traffic

**Attainment survived contact with steering — but only after a fix.** Turning a
live stream up from 200/s to 800/s made it report **182% attainment**: the
denominator was captured once when the stream started, and the numerator was a
lifetime mean. Both halves were wrong. Setting the new target is not enough
either, because the first ten minutes were not a shortfall against a rate that
was only requested in the eleventh.

So the request is now *integrated over time*: each retarget banks what the old
rate owed and starts accruing at the new one. Measured against a real server —
98% at 200/s, still 96% in the instant after the retarget, 99.3% five seconds
later at 800/s. A number that jumps to 400% because somebody moved a slider is
exactly the "measuring the wrong thing" failure attainment exists to prevent.

---

## Template library, completed ✅

**Delivered.** Design document section 70 names eight starter templates; six
shipped with the earlier phases. The last two:

- **`saas-application`** — tenant → account → activity, plus subscriptions.
  The multi-tenancy template: an activity row's `tenant` is the tenant of the
  account *that row chose*, which is the shape every tenant-isolation bug hides
  in. Verified exact — 0 of 4,000 rows disagree with their account's tenant
  when chaos is off. `credits_used` is folded per account, so the hundredth
  event knows what the ninety-nine before it came to
- **`iot-telemetry`** — site → device → sensor → reading. The time-series
  template, and the one where the stateful fold earns its keep: a reading is
  the previous reading plus a small step, not a fresh draw. Measured on one
  sensor's month — mean step between consecutive readings 0.31, mean step if
  the same values are shuffled 1.08. That ratio is the difference between a
  series and noise

**Chaos and a database schema cannot both be had.** Writing the SaaS template
to SQLite failed three times in a row, each time on a different constraint:
`NOT NULL`, then `FOREIGN KEY`, then `UNIQUE` on the primary key. Every one was
entropy injection doing exactly what it was configured to do, and the database
rejecting exactly the defect it had been asked to contain.

The resolution is to write the DDL the data actually satisfies. A chaotic run's
tables carry no keys, uniqueness, `NOT NULL` or `REFERENCES` — a `REFERENCES`
clause pointing at a table with no key is malformed rather than merely
unenforced — and get indexes on the key and reference columns instead, so a
corrupted database is still queryable. `cacophony generate` says so on the way
in, rather than leaving it to be discovered from a constraint that is not
there.

---

# The remaining work

Sections 90–95 of the design document are delivered, and so is everything the
earlier phases left open. What is left is the material that was never assigned
to a phase: nine sections that each add a capability rather than complete one.

They group into five slices. The ordering is by dependency and by risk — the
first improves data people are already generating, the last packages everything
else, and the security-sensitive work sits where it can be given proper
attention rather than squeezed in beside a feature.

| Phase | Sections | Theme |
|---|---|---|
| 10 — Assurance ✅ | 59, 67, 79 | Is what came out any good? |
| 11 — Reuse ✅ | 80, 106, 72 | Fragments, catalogues and bundles |
| 12 — Afterwards ✅ | 105, 104 | Changing a dataset that already exists |
| 13 — Extension ✅ | 44, and `script` | Third-party code, safely |
| 14 — Desktop | 41 | Tauri, without giving up the web |

Section 83 (agentic generation) is out of scope by the document's own framing —
it is headed "Future" and describes a research direction rather than a feature.

---

## Phase 10 — Assurance ✅

**Delivered.** Design document sections 59, 67 and 79.

Three sections that answer the same question from three directions: **is what
came out any good?** Measuring repetition, measuring the model that produced
it, and deliberately producing the worst input an application will ever see.

```bash
cacophony generate project.yaml --edge-cases 0.05
cacophony benchmark project.yaml -m gemma4:12b,qwen3:8b -n 100
```

```yaml
quality:
  duplication:
    max_exact: 0.001
    max_near: 0.02
```

**What was found, rather than what was planned.** Five defects, four of them in
code that looked finished:

*`SequenceMatcher`'s autojunk heuristic.* The `fuzzy` method confirms LSH
candidates with a real sequence ratio. On sequences over 200 characters,
`difflib` silently treats any character appearing in more than 1% of them as
noise — for prose, the vowels. Two biographies differing only in the name scored
**0.014** against a true 0.956, so the confirmation step was throwing away
exactly the duplicates it existed to confirm. `autojunk=False`.

*Bounds that are not constraints.* `age: {generator: random, min: 18, max: 90}`
puts those numbers in the *generator's* options, not in `constraints`. The
edge-case catalogue read only constraints, saw an unbounded integer, and
proposed 2⁶³ as an age. Nothing rejected it — no constraint was violated — and
the dataset would have held a person nine quadrillion years old. Bounds now come
from the generator when the field declares none.

*Edge cases broke cross-field coherence.* Applying them to finished records
produced a colleague whose name was `" leading space"` and whose model-written
biography still began "Courtney specializes in…". Two fields disagreeing is a
broken fixture, not a finding. They are now applied as each field is produced,
so a first name of `O'Brien-Smith` yields `o'brien-smith.smith@example.com` —
which tests the derived field too, and is a better test than either half.

*The benchmark could not fail.* Three models scored 100% on everything, which is
not a benchmark. Real output showed why: a provider enforcing the JSON Schema
natively stops decoding at `maxLength`, so a value is never *over* length — it
is cut mid-word. `"...failed to handle queueing mechanism, led 3"` passes every
check in the platform and is not a sentence. A `CLIPPED` column now measures it,
and the same schema drops from 100% to 70% usable.

*My own sed didn't match.* An intermediate "brutal limits" test was a silent
no-op that made the benchmark look broken when it was fine. Worth recording
because the lesson generalises: a test that cannot fail and a test that is not
running look identical from the outside.

**Bounded, and honest about it.** Exact matching uses a Bloom filter — 18 MB for
ten million values rather than most of a gigabyte. No false negatives, so a
report of zero is exact; the false-positive rate at the load actually reached is
reported beside the count. Near-duplicate detection holds a sliding window
rather than sampling, because model repetition is *local*: measured recall on
name-swapped biographies is 9/9 with zero false candidates on unrelated text,
which a uniform sample of the same size would not achieve.

**The defaults are calibrated, not guessed.** The first pair (`shingle: 5`,
`similarity: 0.85`) would have missed the exact failure section 59 describes.
Measured on a sixty-word biography with only the name changed: trigram Jaccard
0.82; with a clause rewritten too, 0.69; two biographies sharing an opening
sentence and nothing else, 0.13. So `shingle: 3`, `similarity: 0.7` — an order
of magnitude of headroom above the false positives. The LSH band layout is
chosen *below* the target rather than closest to it, because a threshold above
it is a silent false negative.

**Edge cases are not chaos, and the tests enforce it.** Every value is validated
against the field that will hold it; a candidate that does not fit is discarded
and counted. Asserted at a 50% injection rate — five times anything anyone would
run — with zero validation failures across 4,500 records. Keys, unique fields
and references are never touched: an emoji primary key is a broken fixture, not
a robustness test.

**Verified against real servers.** Benchmarked `gemma4:12b`, `gemma4:e4b` and
`smollm3` on a live Ollama host; generated a 24-record dataset with a real model
scoring 100% unique and 29% of records carrying edge cases.

**Not in this phase:** embedding-based duplicate detection. Section 59 names it,
it needs an embedding provider, and no adapter offers one — so declaring the
method raises with an explanation rather than quietly doing something else.

**Found on the way past, and much worse than any of the above.** Adding a field
to `RunConfig` produced no entry in `git status`. `.gitignore` carried a bare
`runs/` for generated run output, and an unanchored directory pattern matches at
every depth — so it also matched `backend/cacophony/runs/`, the Conductor. Its
1,520 lines had never been committed. Every clone since Phase 3 has been unable
to import `cacophony.runs`, and therefore unable to run `generate`, `resume` or
`serve`.

The patterns are now anchored, the package is committed, and a fresh clone runs
all 1,276 tests. The local working tree was fine throughout, which is exactly
why nobody noticed: a repository can be broken in a way that no amount of local
testing will reveal.

---

## Phase 11 — Reuse ✅

**Delivered.** Design document sections 80, 106 and 72.

```bash
cacophony recipes --show employee
cacophony bundle export project.yaml -o team.cacophony
```

```yaml
entities:
  employee:
    count: 5000
    recipes: [employee]     # twelve fields, one line
```

**Recipes expand before validation.** A recipe's fields become ordinary fields,
so the compiler, linter, Studio, patcher and every writer see a normal project
and none of them learns what a recipe is. Attribution is recorded on each field,
`cacophony plan` prints `via employee` beside it, and the Studio badges it —
expansion that cannot be seen is a trap. Overriding one field keeps the rest of
the recipe and its position; naming a different generator drops the old options
wholesale, because merging `llm` onto `template` leaves junk behind. `$self` is
the only substitution, and section 80's manager relationship is what needs it.

**The catalogue is 31 recipes across section 106's five groups**, and every one
is asserted to compile, generate and pass its own validation. A catalogue is a
promise; a recipe that does not work is worse than no recipe.

**Bundles are a trust boundary.** Export follows the paths in the *expanded*
project so a recipe's own lookup table travels with it; a path outside the
project directory is refused rather than dropped. `inspect` verifies every hash
and compiles the bundle without writing anything. Import refuses traversal,
absolute paths, Windows drive letters and symlink entries, checks the whole
archive before writing a byte, and enforces size ceilings. All five hostile
cases are tested, and nothing is left behind.

**Six defects, five of them found by using the thing.** Recipes are the first
feature where writing the content *is* the test, and the content kept finding
bugs:

*Two catalogue recipes could not be used at all.* `email` and `username` read
`first_name` and `last_name` and did not provide them, so `recipes: [email]`
failed to compile. Section 106 lists them as things you reach for, so they now
include a small `name` recipe. Found by the "every recipe compiles" test, which
is exactly why it exists.

*A recipe claimed a uniqueness it could not deliver.* `username` was written
`unique: true`, and two J. Smiths collide — in this dataset and in every real
corporate directory. It would have failed validation on the first run of any
size. It is now honestly non-unique, and the employee id is the key.

*Pattern tokens that do not exist.* `{10000-99999}` is not a token, and three
recipes used one. `cve_identifier` also needed `sequence` rather than a random
pattern: five random digits with `unique: true` collide at better than even odds
over 800 records.

*A self-reference pointed forwards.* Section 80's manager relationship failed
the first time it met an enforced foreign key — a row pointed at a row that did
not exist yet. Self-references now look backwards only, which is also what makes
a management chain acyclic; record zero gets null, because the top of a
hierarchy has no parent. Verified on 300 records: zero forward references.

*A relative path meant "relative to the shell".* A project's own lookup table
was unfindable from any other directory, which makes a portable bundle
impossible: the only paths that would work are absolute, and those are exactly
the paths that cannot travel. Generators now resolve against the schema's
directory, threaded through as `base_dir` and deliberately excluded from
serialisation.

*The first bundle went out without its own CSV*, and `inspect` then reported it
as broken when it was fine. The path lived in the recipe file, not the project,
so collecting references from the authored document missed it; and compiling
`project.yaml` alone could not see the sibling recipe. Export now walks the
expanded project, and `inspect` extracts to a temporary directory and compiles
the bundle as a whole.

**Also found:** the never-linted `runs/` package. `.gitignore` had hidden it
from `ruff` and `mypy` as well as from `git`, so committing it in Phase 10
surfaced seven lint findings in code that had been running in production paths
for eight phases.

---

## Phase 11 — Reuse, as planned

*The plan this replaced, kept because the reasoning still reads true.*

**§80 Generation recipes.** A named schema fragment — "US Corporate Employee
Identity" expands to first name, last name, email, username, employee id and a
manager reference:

```yaml
employee:
  count: 5000
  recipes: [us_corporate_identity]
  fields:
    email: {template: "{first_name|lower}@{company}.example"}   # override one
```

**§106 The catalogue.** Section 106's five groups shipped as built-in recipes:
identity, computing, security, commerce, operational. Most of the underlying
generators exist; what is missing is the packaging that lets somebody write one
line instead of forty.

*The hard part is that expansion must be visible.* A schema that silently gains
eight fields is a schema nobody can debug, so `cacophony plan` has to attribute
every field to the recipe that produced it, and the Studio has to render a
recipe as a group that can be expanded in place. Overriding one field of a
recipe must not require forking it.

**§72 Project portability.** A `.cacophony` bundle: `project.yaml`, `recipes/`,
`templates/`, `workflows/`, `scripts/`, `assets/`, plus a manifest carrying a
version and a content hash. Generated datasets stay outside, as the document
specifies.

*Two hard parts.* Absolute paths — a project referencing `/home/me/names.csv`
or an `--assets-dir` does not survive being sent to somebody else, so export
has to rewrite what it can and refuse loudly about what it cannot. And import
is unpacking an archive somebody sent you: path traversal has to be rejected
rather than trusted.

**Ships with:** `recipes:` on entities, the §106 catalogue, `cacophony recipes`
to list and show them, `cacophony bundle export|import|inspect`, and recipe
attribution through the plan, the API and the Studio.

---

## Phase 12 — Afterwards ✅

**Delivered.** Design document sections 104 and 105.

```bash
cacophony transform out/employee.jsonl --set 'email=mask:8' \
    --where "department == 'Finance'" -o masked.jsonl --record-as mask_finance
cacophony regenerate project.yaml -e employee -r 4823913-4823920
```

```yaml
patches:
  mask_finance_emails:
    entity: employee
    where: "department == 'Finance'"
    set:
      email: "mask:8"
```

**The claim the phase rests on, and the test that earns it.** Transform a file
with a rule; put the same rule in the schema; regenerate. The two are
byte-identical. If they were not, the rule would be a second implementation of
the same intent and one of them would be wrong. Verified on 300 records through
both paths, and again on a real 200-record dataset through the CLI.

That is what makes section 104's "patch rules" a real answer rather than a
euphemism for editing the output. A Cacophony dataset is a pure function of its
schema and its seed; a row edited in a file corresponds to nothing, and the next
`generate` overwrites it without noticing. So the Studio's record editor shows
you a `patches:` block and a Copy button — never a "saved" message about a file
that does not exist.

**One definition of every operation.** Section 105's list lives in
`transforms/operations.py` and is used by the `transform` generator, by
`patches:`, and by the command line. Two copies of `mask` would drift, and the
day they disagreed would be the day somebody's masked column stopped matching the
masked column beside it. The safe-expression allow-lists are shared the same way
and for a stronger reason: two copies of a security boundary means the safer one
is the one nobody is using.

**`add_noise` derives its jitter by hashing rather than drawing.** An operation
that reached for a random number would make a transformed dataset unreproducible
and would change the file every time it ran — so a transform could not be safely
re-run after a failure. Asserted: running the same transform twice produces
identical bytes, and the offset stays inside the percentage asked for across 400
values.

**Nothing is destroyed.** A transform writes beside its target and swaps at the
end, `--in-place` included. Verified by pointing a rule that raises at a file:
the original is untouched and no partial is left behind. Writing over the source
without `--in-place` is refused, and an existing destination needs `--force`.

**It streams.** 124 MB and 400,000 records transformed in 5.4 seconds at **69 MB
peak RSS** — bounded by one record, whatever the file size. Parquet is refused
with a reason rather than half-supported: its records live in column chunks, so a
row-by-row rewrite is a different piece of work and doing it badly would lose the
schema silently.

**Regeneration is nearly free, and that is the point.** Record 4,823,913's seed
is a hash of its position, so it can be produced without the 4,823,912 before it,
without the dataset, and without the run that made it. Verified against a run's
own output, patch rule included. `regenerate` refuses more than a thousand
records and points at `generate`.

**Two defects, both familiar.** `field: str` on a dataclass shadowed
`dataclasses.field` for every later default in the class body — the same bug as
the multimodal phase, fixed the same way with an aliased import. And a CSV
transform shadowed its own writer with the file handle. Neither survived mypy,
which is the argument for having it.

**Not in this phase:** a Studio button that writes the rule into the project
file. The editor produces the YAML and copies it; applying it would go through
the schema patcher, which is Phase 4 machinery that has no route for a
whole-block insert yet.

---

## Phase 12 — Afterwards, as planned

*The plan this replaced.*

These are one feature. Section 104 says that for enormous datasets, editing
rows is inappropriate and the answer is "regeneration, transformations,
filtering, patch rules" — which is section 105.

```bash
cacophony transform out/employee.jsonl \
    --set 'email = mask(email)' \
    --where 'department == "Finance"' \
    --out out/employee.masked.jsonl

cacophony regenerate <run-id> --entity employee --records 4823913-4823920
```

Transforms: lowercase, uppercase, truncate, hash, format date, encode, mask,
normalize, add noise, round, compress. Several exist inside the `transform`
generator already and need lifting out to work on a file rather than a record.

*Regeneration is nearly free* and worth having for that reason alone: record
4,823,913's seed is a hash of its position, so reproducing exactly that record
requires no state, no run and no file — which makes "this one row looks wrong"
a question with a cheap answer.

*The hard part is reproducibility.* A dataset is currently a pure function of
its schema and seed, and an edited row breaks that. So an edit must be recorded
as a **rule in the project** rather than a mutation of the output — the Studio
offers "apply as a patch rule" instead of "save this row" — and a transformed
dataset carries provenance saying which rules were applied to it. Otherwise the
platform's central promise quietly stops being true.

*The second hard part is the usual one:* a transform over forty gigabytes must
stream, not materialise.

**Ships with:** `cacophony transform`, `cacophony regenerate`, a `patches:`
schema block, record editing in the Studio's preview that writes rules, and
transform provenance in the output.

---

## Phase 13 — Extension ✅

**Delivered.** Design document section 44. And a decision, not an
implementation, about `script`.

```toml
[project.entry-points."cacophony.plugins"]
network_packets = "my_package:NetworkPackets"
```

```yaml
requires:
  plugins: [network_packets]
```

**The phase turned on one property, and it is a negative one: Cacophony does not
load Python from a project directory.** A schema arrives by email, in a Git
repository, inside a bundle. If opening one could load its own code, every other
safety property in the platform would be decoration — the expression allow-list,
the bundle importer's refusal of traversal, the linter's careful messages, all
pointless, because the file could simply ask for a shell.

So discovery is installed entry points and only entry points, and that is
asserted directly rather than assumed: a test writes Python into `plugins/`,
`cacophony_plugins/` and `extensions/` beside a schema, generates from it, and
checks the marker file was never written. A second test greps the loader for
`glob`, `rglob`, `iterdir`, `listdir` and `spec_from_file` and fails if any
appears. The trust decision belongs to a person running `pip install`.

**All eight categories reach a registry that already existed** — generators since
phase one, providers since phase two, output formats since phase one, transform
operations since phase twelve. A plugin is a door into an extension point, not a
mechanism beside one. Two new hooks were added for the two categories that had
none: `extra_validators()` and `extra_scenarios()`.

**The manifest is a contract checked in both directions.** Registering something
undeclared has it refused; declaring something and never registering it is
reported missing. Neither is a security measure — a plugin is code you installed
— but a manifest that has drifted from its code produces a project that works on
one machine and fails on another with no clue why. A plugin may not take over an
existing name without `replace = True`: one that silently replaced the built-in
`uuid` would change every project on the machine.

**Verified against a really installed package.** Section 44's own example, built
as `cacophony-netpackets`, `pip install`ed, and used in a project that requires
it: the plugin contributed a generator *and* a transform operation, and the
transform was consumed by a Phase 12 patch rule. Then uninstalled, so the test
suite does not depend on what happens to be on this machine — the tests use fake
entry points, and the one CLI test that needed an empty environment patches the
discovery function rather than assuming one.

**The Studio's Plugins page exists**, which retires the last placeholder in
section 46's navigation. The helper that stood in for later-phase destinations is
gone with it.

### `script` stays refused, and that is now a decision

The plan for this phase said: *if the sandbox turns out not to be affordable,
`script` stays declared and refused*. It is not affordable, so it does.

*A restricted interpreter is not a sandbox.* Stripping `__builtins__` and denying
imports is a denylist, and denylists on a language with introspection are
routinely escaped through object graphs nobody anticipated. Shipping one would
invite exactly the trust it cannot support.

*What isolation is available was measured rather than assumed.* Unprivileged user
and network namespaces do work on this host — a subprocess in one cannot open a
socket, confirmed. But the filesystem stays fully readable, and blocking it needs
a mount namespace and a pivot into an empty root: Linux-only machinery,
untestable on the macOS and Windows the desktop phase targets. A security
boundary that exists on one of three platforms is not a boundary.

*WebAssembly is the honest option and is out of scope here.* A CPython build for
a WASM runtime gives real isolation with memory and fuel ceilings enforced by the
runtime. It is also a multi-megabyte dependency and substantial work, and it is
how this should be done when it is done.

The refusal message now says "deliberately not implemented" and points at the
three alternatives — `expression`, `patches`, a plugin — rather than promising a
phase that is not planned. Fixing it also surfaced a small long-standing wart:
the engine prefixed the field location onto an error that already carried it, so
the message read `row.x: row.x: …`.

---

## Phase 13 — Extension, as planned

*The plan this replaced.*

Section 44's eight plugin categories — `GeneratorPlugin`, `ValidatorPlugin`,
`TransformPlugin`, `OutputPlugin`, `LanguageModelPlugin`, `ImagePlugin`,
`SpeechPlugin`, `ScenarioPlugin` — each already have a registry to hook into.
The `script` generator has been declared and refused since Phase 1 for exactly
the reason this phase exists.

**The whole phase is one question: a project file is something people share.**
Loading Python from a project directory makes opening a schema somebody sent
you equivalent to running their code. Everything else here is straightforward;
this is not, and it is why this work sits late rather than early.

The plan is to answer it in two steps, in this order:

1. **A trust boundary for plugins.** Plugins are discovered from installed
   entry points — packages somebody chose to `pip install` — and never
   auto-loaded from a project directory. A manifest declares what a plugin
   provides; the registry refuses anything it did not declare. A project may
   *require* a plugin by name and version, which fails loudly if it is absent,
   rather than carrying its code.
2. **A real sandbox for `script`.** A WebAssembly runtime is the honest option:
   no filesystem, no network, no host imports, a memory ceiling and a fuel
   limit, all enforced by the runtime rather than by a denylist. A subprocess
   with seccomp is cheaper and platform-specific; a restricted-`exec` sandbox
   is not a sandbox and will not be built.

*If the sandbox turns out not to be affordable, `script` stays declared and
refused.* That is a better outcome than a `script` field which is unsafe to
open, and the `expression` generator already covers derived values safely.

**Ships with:** plugin manifests and discovery, `cacophony plugins list|info`,
a `requires:` block in the schema, the Studio's Plugins page (currently a
placeholder), and either a sandboxed `script` or a documented decision not to
ship one yet.

---

## Phase 14 — Desktop

*Design document section 41.*

A Tauri shell hosting the built Studio, with the Python backend as a sidecar
process. Section 41 prefers Tauri to Electron because the application mainly
needs to host a web UI while Python does the generation — which is exactly the
architecture that already exists.

*The hard part is not the shell, it is the runtime.* Shipping a Python
interpreter per platform (PyInstaller or PyOxidizer), signing and notarising on
macOS, signing on Windows, and keeping the sidecar's lifetime tied to the
window so closing the app does not leave a generator running.

*The constraint the document sets is the one to hold onto:* "web deployment
should remain possible". `cacophony serve` has to keep working identically, so
the desktop build must be a shell around the same server rather than a fork of
it.

**Ships with:** a Tauri application, bundled backends for macOS, Windows and
Linux, a first-run experience that does not mention Python, and `cacophony
serve` behaving exactly as it does today.

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

**Media paths are derived, not allocated (sections 32, 65).** An asset's path
comes from its entity, record index and field, which means it can be computed
*before* the expensive part. A resumed run therefore asks "is this portrait
already on disk?" and skips a thirty-second diffusion call — the second run of
a portrait-heavy project is hundreds of times faster than the first. That
saving is reported explicitly (`64 already on disk`) rather than showing up as
a suspiciously quick run, because an optimisation nobody can see is one nobody
trusts.

**Why Cacophony writes its own PDFs.** Usually the wrong instinct. What is
needed here is a page of text in one of the fourteen fonts every reader has
built in: no images, no embedded fonts, no colour management. That is a few
hundred lines of a well-documented format, against a dependency most users
would never touch. The moment a project needs real typesetting, an output
plugin is the right seam — not a heavier default for everyone.

**Document templates are not Jinja2 (section 23).** A document template ships
inside a project file that people share, and section 74 wants those files
reviewed in Git like code. A template language with expressions in it would
make opening a shared project equivalent to running a stranger's code, which
is the same reason `script:` remains unimplemented. `{field}` and
`{related.field}` substitution covers invoices, badges and transcripts.

**Chaos and validation had to be told about each other (section 24).** The
first chaotic run reported 21,250 validation failures out of 80,066 records —
validation catching, faithfully, exactly the damage that had been asked for.
With `--drop-invalid` it would have discarded precisely the records the user
wanted. So an injector marks the fields it damaged, the validator skips those
and checks the rest, and a record that *is* a deliberate duplicate is exempt
from the uniqueness check it would otherwise fail by construction. Damage lives
in `GeneratedRecord.damage` rather than in `values`, so it never appears as a
column.

One thing is protected from chaos besides the primary key: a `scenario` field.
Those are Cacophony's own annotation of what it did, and a dataset that has
been deliberately corrupted still has to be able to tell you which records were
corrupted and why.

**A world is deliberately thin (section 16).** Because a record is derived by
hashing its position, a schema plus a seed already *is* a world — run it twice
and the same five thousand people come out. So a world stores a name, the seed,
a content hash of the fields, and the population sizes; nothing that could be
recomputed is kept, which is what section 42 asks. What it buys is a name, a
warning when the schema has moved on, and a record of which runs drew from it.
`--world` and `--seed` together are refused rather than resolved, because the
one thing that must never happen quietly is a run producing *different* people
under a world's name.
