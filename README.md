# Cacophony

**A synthetic reality compiler.**

Cacophony generates enormous quantities of realistic, structured, internally
consistent synthetic data from schemas that describe what data *means* rather
than how to fabricate it.

You describe the shape and the meaning. Cacophony decides how to generate it.

The full design is in [CACOPHONY.md](CACOPHONY.md). This README covers what is
built and how to run it.

---

## Status: Phase 13 — Extension

Phases 1–7 built the engine, the providers, the run system, the Studio, the
relational layer, multimodal generation and synthetic worlds. Phase 8 turned
Cacophony into a workload generator; Phase 9 spread a run across machines, with
byte-identical output. Phase 10 asked whether what came out is any good:
repetition, the model that produced it, and what an application does with valid
but awkward data. Phase 11 made schemas reusable — a catalogue of
fragments, and projects that can be sent to somebody else. Phase 12 dealt with a dataset that already
exists and is not quite right. Phase 13 lets other people's code extend the
platform — and settles what `script` is, which is nothing.

**Working now**

- Project schemas in YAML or JSON, with entities, fields, semantic annotations,
  types, constraints, chaos settings and output profiles
- Schema compiler: generator resolution, dependency graph, cycle detection,
  entity ordering, generation plan, workload estimates
- Schema linter with the checks from design document section 102
- Generator recommendation engine — a field with only a semantic description
  still gets a sensible generator instead of an expensive language-model call
- 30 registered generators: constant, sequence, uuid, random, boolean,
  distribution, weighted, lookup, pattern, template, expression, datetime, ip,
  mac, phone, government_id, faker, composite, transform, null, reference, llm,
  image, tts, document, event_time, subject, state and scenario, plus the
  pending script declaration
- Hierarchical deterministic seeds — record *n* is identical whether generated
  first, last, in parallel, or after a resume
- Structural and constraint validation with repair
- Streaming exporters: CSV, JSON, JSON Lines, Parquet, SQLite and SQL scripts —
  the last two carrying the foreign keys as constraints the database enforces
- Foreign keys that cost no memory: a parent is computed from its index rather
  than kept in a pool, so a hundred million events pointing at five thousand
  employees is the same amount of state as five
- Reference distributions — `skewed` by default in the templates, because real
  activity concentrates and uniform references produce data that is valid and
  behaves like nothing
- Referential and statistical validation: sampled foreign-key checks, and
  generated distributions compared against declared ones
- Image, audio and document fields — InvokeAI, Piper and any
  `/v1/audio/speech` server, plus procedural providers that need no GPU
- An asset store that derives every path, stores identical bytes once, and
  records what each file belongs to and how it was made
- Temporal simulation — a period and a shape, so activity follows the working
  week, the working day, the season and the holidays
- Stateful simulation — running balances and counters, folded per subject and
  replayed rather than persisted, so a resumed run agrees with an uninterrupted
  one
- A scenario engine — a fraction of subjects caught up in an incident, in
  ordered phases, correlated across every entity that references them
- Entropy injection — five presets of deliberate damage, recorded in provenance
  and exempt from validation
- Named worlds — `--world acme` generates the same five thousand people into
  every dataset you make from it
- Continuous streaming to stdout, a rotating file, syslog, HTTP or Kafka, at a
  rate you set and can change while it runs
- Distributed generation — a controller cuts a run into shards and leases them
  to workers that advertise what they can do, reassigning any shard whose
  holder goes quiet; the joined result is byte-identical to a single-machine
  run, verified including after a worker is killed mid-shard
- Duplicate detection — exact, normalised, n-gram and fuzzy, in bounded memory:
  18 MB of Bloom filter for ten million values, and a sliding MinHash window,
  because a model that hands back the same biography with the name changed
  produces values nothing else in the platform can see are the same
- A model benchmark — `cacophony benchmark -m a,b,c` generates real records
  through the real pipeline and reports validity, usability, clipping, speed and
  repetition, with the cache forced off so nobody is scored on somebody else's
  answers
- A plugin protocol — section 44's eight categories, discovered through
  installed entry points and never from a project directory, with manifests
  checked against what a plugin actually registers
- Post-generation transforms — mask, hash, round, jitter, coarsen a date,
  encode, compress, filter; streaming, so a 124 MB file transforms at 69 MB of
  memory, and never writing over its input until the new file is complete
- Patch rules — an edit recorded in the project rather than made to a file, so
  it applies on every run and survives a regeneration. Verified: transforming a
  file and regenerating with the same rule produce identical bytes
- `cacophony regenerate` — re-derive record 4,823,913 on its own, with no run,
  no file and no state, because its seed is a hash of its position
- Generation recipes — `recipes: [employee]` expands to the twelve fields
  everybody writes the same way, with attribution on every one so expansion is
  never invisible; override a field without forking the recipe
- A catalogue of 31 recipes across identity, computing, security, commerce and
  operational, every one asserted to compile, generate and validate
- Portable `.cacophony` bundles — a project, its recipes, its templates and the
  files its schema references, with hashes; import refuses path traversal,
  absolute paths and symlinks, and writes nothing until the whole archive checks
  out
- Edge-case generation — a QA mode that produces *legal* but awkward values:
  `O'Brien-Smith`, `Ω`, emoji with zero-width joiners, RTL overrides, leap days,
  DST boundaries, the antimeridian. Every one validated against the field that
  holds it, because an edge case that fails validation proves nothing
- Language-model generation against Ollama, llama.cpp and any
  OpenAI-compatible server, addressed by URI
- The Prompt Compiler — you write what a field *means*, it writes the
  instruction and the JSON Schema that constrains the answer
- Structured output enforcement: extract, parse, validate, repair, retry
- Four generation modes: per-field, per-record, batch, contextual expansion
- Content-addressed cache, so a re-run costs nothing it has already paid for
- Credentials resolved from the environment or the OS keychain, never from the
  project file
- Durable runs: one job per entity, checkpointed after every batch, resumable
  from an interruption without duplicating or skipping a record
- Run history in a SQLite store beside the project, recording the exact schema
  revision each run used
- REST API and a WebSocket feed of live progress, for runs and for streams —
  including `retarget`, so a workload can be turned up while you watch what it
  does to whatever is receiving it
- Structured logging with the fields design document section 86 asks for
- CLI: `validate`, `lint`, `plan`, `prompt`, `propose`, `preview`, `generate`,
  `resume`, `runs`, `run`, `serve`, `stream`, `cluster`, `controller`,
  `worker`, `worlds`, `benchmark`, `recipes`, `bundle`, `transform`,
  `regenerate`, `plugins`, `generators`, `providers --test`, `models`
- **Cacophony Studio**: project dashboard, schema editor with live preview,
  distribution and relationship views, a generate screen with cost estimates,
  a live run view fed by the WebSocket, a streaming page with per-entity rate
  controls and the records going past, and an asset browser that shows the
  images, plays the audio and opens the documents
- Schema edits are applied as targeted patches, so a documented YAML file keeps
  its comments, its ordering and its formatting
- `cacophony propose "…"` — a description of a domain becomes a compiled,
  linted schema file. The model proposes the structure; Cacophony picks the
  generators and refuses to hand back anything that does not compile

**Declared and deliberately not implemented.** `script` (section 8) would run
code from a project file, and a project file is something people share. No
sandbox available is trustworthy on all three platforms Cacophony targets, so
after measuring the options the plugin phase decided against shipping one — see
[docs/schema-reference.md](docs/schema-reference.md#the-script-generator) for the
reasoning. Use `expression` for a derived value, `patches` for a per-record
transformation, or a plugin for real code. A `script` field still compiles, lints,
plans and estimates, and `on_unavailable: placeholder` runs the whole pipeline
with obviously-marked stand-ins.

**Still to come.** One slice: Tauri desktop packaging (14). Everything else in
the design document is either delivered or, in the single case of `script`,
decided against with the reasoning written down.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the phase plan and what makes each
one hard.

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

### Data that joins

`templates/retail-commerce.yaml` is four entities pointing at one another:
customer → order → order_item → product. Written as a database, the
relationships become constraints:

```bash
cacophony generate templates/retail-commerce.yaml -o sqlite -d out/

sqlite3 out/retail-commerce.db "PRAGMA foreign_key_check"   # silent
sqlite3 out/retail-commerce.db "
  SELECT p.category, ROUND(SUM(i.line_total), 2) AS revenue
  FROM order_item i JOIN product p ON i.product = p.sku
  GROUP BY p.category ORDER BY revenue DESC"
```

Every key resolves, and it costs nothing to make that true. A record's seed is
derived from its *position*, so parent 4,823,913 can be reconstructed directly
— a foreign key is arithmetic on an index rather than a lookup in a table Cacophony
had to keep. A hundred million events pointing at five thousand employees is
the same amount of memory as five.

The derived fields follow the reference rather than the entity:

```yaml
order:
  fields:
    customer:
      generator: reference
      entity: customer
      distribution: skewed      # a few customers place most of the orders
    ship_to_country:
      generator: expression
      expression: "customer.country"    # *this* row's customer
```

The run reports whether it kept its promises:

```
complete  173,400 records in 25.00s
  referential     100.00%  (285,000 references checked)
  distributions   99.04% match
  references      570,000 resolved, 91% from cache
```

### One record, several artifacts

`templates/multimodal-support.yaml` gives every employee a portrait and an ID
badge, and every support call a recording and a transcript:

```bash
cacophony generate templates/multimodal-support.yaml -d out/

ls out/assets/employee/00000000/
#   employee_00000000_id_badge.pdf   employee_00000000_portrait.png
pdftotext out/assets/employee/00000000/employee_00000000_id_badge.pdf -
#   MULTIMODAL SUPPORT CENTRE
#   Denise Garcia
#   SUP-0001
```

Each file is derived from the record it hangs off — the badge carries that
employee's own name and number, the recording speaks that call's own
transcript. The run says what it produced:

```
complete  32 records in 5.24s
  referential     100.00%  (20 references checked)
  assets          64 files, 5.1 MB
                  11 deduplicated, 0 already on disk
```

Run it again and the second line reads `64 already on disk`: asset paths are
derived from the record's position, so nothing is generated twice.

It needs no GPU and no model server. The `procedural_image` and
`procedural_speech` adapters draw and synthesise in-process — deliberately
obvious stand-ins, labelled `synthetic: true`, so nothing is mistaken for a
photograph or for speech.

Point the same schema at real hardware by changing the two adapter blocks.
Nothing below `providers:` moves:

```yaml
providers:
  pictures:
    type: image
    adapter: invokeai            # was: procedural_image
    base_url: http://diffusion-box:9090
    model: Dreamshaper 8

  voices:
    type: speech
    adapter: piper               # was: procedural_speech
    base_url: http://tts-box:5000
```

The same twelve employees then have diffusion portraits and the same twenty
calls have spoken recordings. Both adapters are tested against real servers —
InvokeAI 6.13.8 and piper1-gpl — by the live contract tests in
`tests/test_provider_contracts.py`.

### A dataset something happened in

`templates/security-operations.yaml` is four entities and eight scenarios.
Every sign-in belongs to an identity, arrives in order within that identity's
own history, and follows the working week — and a fraction of a per cent of
those identities are having a bad month:

```bash
cacophony generate templates/security-operations.yaml -d out/

# one compromised identity's month
jq -r 'select(.scenario=="ransomware" and .user=="USR-001482")
       | [.timestamp[:16], .phase, .application, .result] | @tsv' \
    out/authentication.jsonl | sort
```

```
2026-02-27T11:13  initial_access     VPN Gateway      success
2026-03-02T05:16  credential_access  VPN Gateway      failure_bad_password
2026-03-03T13:32  credential_access  Microsoft 365    success
2026-03-04T12:24  lateral_movement   Internal Wiki    success
2026-03-05T14:11  lateral_movement   Internal Wiki    success
2026-03-06T10:58  encryption         Admin Console    success
```

That is section 17's timeline, for one identity, over eight days. The same
identities are compromised on every run — subject selection is a hash of the
seed and the scenario's name, not a sample — and every entity that references
them agrees about who was involved.

The run says what it built:

```
complete  136,414 records in 45.56s
  referential     100.00%  (119,854 references checked)
  scenarios       337 records affected
                  phishing 185, password_spray 107, ransomware 27, impossible_travel 18
  simulation      authentication: 120,000 events over 4,800 user
  chaos           3,461 records damaged, 150 duplicated
```

**Events are ordered without being sorted.** The timeline compiles its shape
into a cumulative distribution, and the *k*-th of a subject's *n* events is
drawn at quantile *k/n* — so a subject's timestamps come out chronological with
no sort, no memory, and the same answer whichever record is generated first.

**Balances survive a resume.** A running total is a fold over one subject's
events, and subjects get contiguous blocks, so restarting at event 4,823,913
replays that account's block — a few dozen records — rather than the dataset.

**Damage is deliberate and labelled.** `chaos: {preset: realistic}` nulls
fields, mangles text, injects zero-width spaces and duplicates records. Each
defect is recorded in `_chaos`, and the validator skips what was damaged on
purpose — otherwise a chaotic run is just a wall of validation failures.

### A workload generator

```bash
cacophony stream templates/security-operations.yaml \
    --rate authentication=250/s --rate security_finding=8/minute \
    --to syslog://siem.internal:514
```

```
CACOPHONY streaming running                   251/s of 250/s  (100%)
18,204 generated · 18,204 delivered · 73s

  authentication 250/s                                18,024
  security_finding 8 per minute                          180
  → syslog                                            18,204
```

Rates are written the way people say them — `250/s`, `8 per minute`,
`1200/hour` — and destinations are URIs: `stdout`, `file://logs.jsonl`,
`syslog://host:514`, `https://host/ingest`, `kafka://broker:9092/topic`.
Several `--to` flags feed the same records to every one.

Because a record's seed comes from its index, a stream is still reproducible
and still resumable: `--from N` continues the sequence rather than replaying
it, and the summary tells you the number.

Piping works, because the dashboard goes to stderr:

```bash
cacophony stream project.yaml -r authentication=200/s -t stdout \
  | jq -r 'select(.risk_score > 80) | [.timestamp[11:19], .application, .result] | @tsv'
```

**Attainment is the number that matters.** The dashboard shows achieved against
requested, and warns below 95%. A workload generator that reports "18,204
delivered" while quietly running at sixty per cent of the rate you asked for is
measuring the wrong thing.

The same stream over HTTP, where you can also *steer* it:

```bash
curl -X POST localhost:8765/api/projects/1/streams -H 'content-type: application/json' \
  -d '{"rates": {"authentication": "250/s"}, "destinations": ["syslog://siem:514"]}'

# Turn it up while it runs, and watch what that does to the collector
curl -X POST localhost:8765/api/streams/$ID/retarget -H 'content-type: application/json' \
  -d '{"entity": "authentication", "rate": "2000/s"}'
```

The Studio's streaming page is that, with the rates as fields you edit, the
per-destination delivery counts beside them, and a window of the records going
past. Attainment integrates the request over time, so turning a stream up does
not make it claim it was over-delivering all along.

### Many machines, the same bytes

```bash
# One machine, several workers
cacophony cluster templates/security-operations.yaml -o out/ --workers 8

# Or across a cluster: one controller, any number of workers
cacophony controller templates/security-operations.yaml --port 8787
cacophony worker templates/security-operations.yaml \
    -c http://controller:8787 -o /mnt/shared
```

```
progress  100.0%  24,022 / 24,000
    rate  8,410/s
  shards  completed 12
 workers  worker    capabilities             shards  records     rate  state
          gpu-node  deterministic, image          4    8,000  2,904/s  working
          cpu-1     deterministic                 4    8,011  2,752/s  working
          cpu-2     deterministic                 4    8,011  2,754/s  working
```

A shard is an index range. A worker leases one, generates it, writes
`entity.part000050000.jsonl`, and says how many records it made. Shards needing
a GPU are only ever offered to nodes that have one — a shard's requirements are
read off its compiled generators, and a worker's off its configured providers,
so nobody declares either.

**The joined output is byte-identical to a single-machine run.** Not by
careful merging — by construction. Record *n*'s seed is a hash of *n*
(design document section 75), so record 4,823,913 is the same record on any
machine, in any order, at any time. Shards tile the entity exactly once, and
concatenating them in offset order reproduces the same byte stream.

Which makes failure cheap. If a worker dies mid-shard its lease expires, the
shard is handed to somebody else, and that worker regenerates it from
scratch — producing exactly the bytes the dead one would have. Tested by
`kill -9`ing a worker in the middle of a shard: the dataset still matched,
half-written file and all.

### Somebody else's code

```toml
# In their package, not in your schema.
[project.entry-points."cacophony.plugins"]
network_packets = "my_package:NetworkPackets"
```

```yaml
requires:
  plugins: [network_packets]
```

```
$ cacophony plugins
plugin           version  provides                    state
network_packets  1.0      generators 1, transforms 1  loaded

contributed
  generators       network_packet           network_packets
  transforms       rot13                    network_packets
```

**Cacophony never loads Python from a project directory**, and that is the
feature. A schema arrives by email, in a Git repository, inside a bundle — if
opening one could load its own code, every other safety property here would be
decoration. Plugins come from packages you chose to `pip install`, so the trust
decision sits with a person at install time rather than with a program at open
time.

All eight of section 44's categories reach a registry that already existed:
generators, transforms, output formats, validators, scenarios, and the three
kinds of provider. A manifest is a contract checked both ways — register
something you did not declare and it is refused; declare something and never
register it and you are told.

### Changing a dataset that already exists

```bash
cacophony transform out/employee.jsonl \
    --set 'email=mask:8' --where "department == 'Finance'" \
    -o out/masked.jsonl --record-as mask_finance_emails
```

```
rule   cli where department == 'Finance' set email = mask:8

wrote  out/masked.jsonl
  read            400,000
  written         400,000
  edited           26,113  (26,113 values)

as a patch rule
  patches:
    mask_finance_emails:
      where: department == 'Finance'
      set:
        email: mask:8
```

**It prints the rule because the file is the wrong place to keep the change.** A
Cacophony dataset is a pure function of its schema and its seed; a row edited in
an output file corresponds to nothing, and the next `generate` overwrites it
without noticing. Paste that block into the project and the change applies on
every run — and transforming the file and regenerating from the schema produce
*byte-identical* output, which is the property that makes patch rules a real
answer rather than a euphemism.

It streams: 124 MB and 400,000 records in 5.4 seconds at 69 MB of memory. And it
never writes over its input until the new file is complete, `--in-place`
included.

```bash
# No run, no file, no state — record 4,823,913's seed is a hash of its position.
cacophony regenerate project.yaml -e employee -r 4823913-4823920
```

### One line instead of forty

```yaml
entities:
  employee:
    count: 5000
    recipes: [employee]          # section 80's example, as one line
    fields:
      email:                     # override one without restating the rest
        template: "{first_name|lower}.{last_name|lower}@acme.example"
```

```
$ cacophony plan project.yaml
   Generate 5000 employee
     first_name       faker(first_name)                        via name
     last_name        faker(last_name)                         via name
     full_name        template({first_name} {last_name})        via person
     email            template(…@acme.example)                  via email
     username         expression(lower(substr(first_name…)))    via username
     employee_id      sequence(EMP-{000000})                    via employee
     manager          reference(employee.employee_id, skewed)   via employee
```

`via` is not decoration. A schema that silently gains twelve fields is a schema
nobody can debug, so every expanded field says where it came from — in the plan,
in the API, and in the Studio's field editor.

`cacophony recipes` lists the 31 in the catalogue: identity, computing, security,
commerce, operational. Every one is asserted to compile, generate and pass its
own validation, because a catalogue is a promise.

### Send it to somebody

```bash
cacophony bundle export project.yaml -o team.cacophony
cacophony bundle inspect team.cacophony     # verifies hashes, compiles, writes nothing
cacophony bundle import team.cacophony -d ./team
```

A bundle carries what *makes* a dataset, never the dataset — section 72 is
explicit, and a project is kilobytes while a dataset is gigabytes. It follows
the paths your schema references, so a lookup table in `data/` travels too; a
path outside the project directory is refused rather than silently dropped.

Import treats the archive as untrusted input: traversal, absolute paths, drive
letters and symlink entries are refused, and nothing is written until the whole
archive checks out.

### Is it any good?

Three questions the rest of the platform cannot answer.

**Did the model repeat itself?** It usually does, and nothing else can see it —
the same biography with the name changed is a unique string, the right type, no
constraint violated.

```yaml
quality:
  duplication:
    max_near: 0.02
```

```
duplication     colleague: 0.33% unique  (0 repeated, 299 near, over 300 values)
                compared biography by exact, minhash
                colleague: 99.67% of values are near duplicates, above the 10.00% allowed
                e.g. biography at record 1: Darryl Vargas is a seasoned engineering manager…
```

Bounded: 18 MB of Bloom filter for ten million values, and a sliding MinHash
window. No false negatives, so a report of zero is exact — and the
false-positive rate is printed beside any non-zero count rather than left
implied.

**Which model should you use?**

```bash
cacophony benchmark project.yaml -m gemma4:12b,smollm3 -n 100
```

```
MODEL               VALID  FIELDS  USABLE  CLIPPED   SPEED  DUPLICATION  LATENCY
gemma4:12b         100.0%  100.0%   91.7%        1  11 t/s         0.0%  1760 ms
smollm3            100.0%  100.0%   70.0%        6  21 t/s         0.0%   806 ms
```

Real records, real pipeline, same seed for every model, cache forced off. The
`CLIPPED` column exists because real output demanded it: a provider enforcing the
JSON Schema stops decoding at `maxLength`, so a value is never *over* length — it
is cut mid-word, passes every check, and is not a sentence.

**What does your application do with valid but awkward data?**

```bash
cacophony generate project.yaml --edge-cases 0.05
```

```
first_name "O'Brien-Smith"        →  email  "o'brien-smith.smith@example.com"
first_name 'شركة الاتحاد للتجارة'  →  email  'شركةالاتحادللتجارة.fuller@example.com'
```

Not chaos. Chaos produces data the schema forbids and asks what your pipeline
does with garbage; these are real surnames, and an application that cannot store
them has a bug. Every value is validated against the field that holds it, and
they are applied *as each field is produced* — so the derived email tests the
template too.

### The same people, twice

```bash
cacophony worlds project.yaml --create acme
cacophony generate project.yaml --world acme -e authentication -d logs/
cacophony generate project.yaml --world acme -e ticket -d tickets/
```

Employee `USR-000001` is the same person in both, because a schema plus a seed
already *is* a world — a world just gives it a name, warns when the schema has
moved on, and refuses `--seed` alongside it.

### Describing a schema instead of writing one

```bash
cacophony propose "employees, company laptops, login activity and security
  alerts for a 5,000-person company" -m llama3.1:8b --out security.yaml
```

This one needs a model. It talks to Ollama on `localhost:11434` by default;
`--adapter`, `--url` and `-m` point it elsewhere, and `--providers
project.yaml` borrows the configuration from a project you have already set up.

The model proposes the entities, their fields, what each field means and which
entity points at which. Cacophony chooses the generators — it knows that a
field called `email` wants Faker on a reserved domain, and it will not invent a
generator that does not exist. The proposal is compiled and linted before you
see it; one that does not compile goes back to the model with the error
attached, and if that fails too, nothing is returned.

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

### Runs that survive being interrupted

```bash
# Start something long. Ctrl-C it, pull the power, fill the disk.
cacophony generate templates/security-operations.yaml -o parquet -d out/

# See what happened
cacophony runs
cacophony run 4f2a91c3            # the inspector: jobs, quality, output

# Carry on from the last checkpoint
cacophony resume 4f2a91c3
```

A resumed run continues under the schema revision it *started* with, not
whatever the file says now — otherwise the dataset would be generated two
different ways. Fix a schema and you start a new run; the store keeps both
revisions.

### The Studio

```bash
cd frontend && npm install && npm run build && cd ..
cacophony serve --port 8765        # Studio at /, API docs at /docs
```

During front-end development, run the Vite server instead — it proxies the API
and the WebSocket to the backend, so both halves talk to the same routes:

```bash
cacophony serve &                  # terminal one
cd frontend && npm run dev         # terminal two, on :5173
```

### Serving the API alone

```bash
cacophony serve --port 8765        # docs at /docs
```

```
POST /api/projects                 GET  /api/projects/{id}/plan
POST /api/projects/{id}/runs       POST /api/projects/{id}/preview
GET  /api/runs/{id}                POST /api/runs/{id}/pause
POST /api/runs/{id}/resume         POST /api/runs/{id}/cancel
GET  /api/runs/{id}/quality        GET  /api/projects/{id}/schema
WS   /api/runs/{id}/stream         GET  /api/providers/{id}/models
POST /api/projects/{id}/streams    POST /api/streams/{id}/retarget
GET  /api/streams/{id}/records     WS   /api/streams/{id}/feed
```

## Templates

Design document section 70's eight starter projects. Every one compiles, lints
and generates with no model server.

| Template | Shape | What it shows |
|---|---|---|
| `corporate-directory` | employees, devices, locations | The basics, and semantic annotation |
| `retail-commerce` | customer → order → order_item → product | Foreign keys that resolve; a line item with two parents |
| `helpdesk` | users, devices, tickets | Model-written fields degrading to marked placeholders |
| `security-operations` | users, devices, authentication, findings | Timelines, per-subject histories and executing scenarios |
| `saas-application` | tenant → account → activity, subscriptions | Multi-tenancy carried through a join; money folded per account |
| `iot-telemetry` | site → device → sensor → reading | A time series that is a random walk rather than noise |
| `conversational-ai` | users, conversations, labelled intents | The prompt compiler and structured output |
| `multimodal-support` | employees with portraits and badges, calls with audio | Images, speech and documents hanging off records |

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
├── schema/        project model, compiler, graph, plan, linter, recipes, bundles
├── generation/    generator registry, built-in generators, recommendation, engine
├── validation/    structural and constraint validators, duplicate detection
├── transforms/    section 105's operations, patch rules, the file pipeline
├── outputs/       CSV, JSON, JSONL and Parquet writers
├── runs/          the Conductor: jobs, checkpoints, pause/resume/cancel
├── store/         SQLite metadata: projects, schema revisions, runs, jobs
├── observability/ structured logging and run metrics
├── providers/     language-model adapters, cache, secrets, provider registry
├── assets/        the asset store, imaging, audio and document rendering
├── simulation/    timelines, subject allocation, state folds, scenarios, chaos
├── live/          rates, sinks and the continuous stream
├── distributed/   controller, workers, leases, capabilities, assembly
├── scenarios/     scenario declarations and the plugin hook
├── plugins/       the plugin protocol: manifests, host, entry-point discovery
├── api/           REST API, the live run feed, and the built Studio
└── cli/           command-line interface
frontend/          Cacophony Studio (React, TypeScript, Vite)
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
pytest                  # backend tests
ruff check .            # lint
ruff format .           # format
mypy                    # type check

cd frontend
npm test                # Studio tests
npm run typecheck       # TypeScript
npm run build           # build into the package
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

**The Studio never rewrites your schema.** A form that saved by
re-serialising its own model would produce a correct file with every comment
and every deliberate ordering stripped out. Edits are sent as targeted
operations and applied to the YAML document in place, so the only thing that
changes is the thing you changed (sections 48, 74).

**A checkpoint is one integer.** Because a record's seed is derived from its
index, resuming needs to know only how many records a job finished — there is
no RNG state to serialise. And because a stale checkpoint would silently
duplicate records, the file on disk is counted and the checkpoint corrected to
match before anything is appended (section 32).
