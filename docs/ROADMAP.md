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
