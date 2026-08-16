# Schema reference

The complete project schema as of Phase 2. See [CACOPHONY.md](../CACOPHONY.md)
for the design rationale and [ROADMAP.md](ROADMAP.md) for what is not built
yet.

A project is one YAML or JSON file with up to six top-level keys:

```yaml
project:        # metadata, seed, locale, provenance mode
entities:       # the record types to generate
relationships:  # connections between entities
providers:      # generation backends
scenarios:      # behavioural overlays
chaos:          # entropy injection
outputs:        # output profiles
```

Only `project` and `entities` are required.

---

## `project`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | *required* | Project name |
| `description` | string | – | Free text |
| `version` | integer | `1` | Schema revision, recorded in provenance |
| `seed` | integer | `0` | Root of the seed hierarchy |
| `locale` | string | `en_US` | Default Faker locale |
| `profile` | enum | `balanced` | `quick_mock`, `balanced`, `high_realism`, `maximum_chaos` |
| `provenance` | enum | `none` | `none`, `run`, `record`, `field`, `full` |

Pin `seed` in any project you intend to reproduce. Leaving it at `0` still
produces deterministic output, but a schema that states its seed explicitly
says so to the next reader.

---

## `entities`

```yaml
entities:
  employee:
    count: 10000
    description: A person employed by the company.
    primary_key: employee_id
    seed: 991                 # optional: regenerate this entity independently
    tags: [core]
    fields: { ... }
```

`count` is the number of records. `cacophony generate --records N` overrides it
for every entity.

---

## Fields

```yaml
fields:
  employee_id:
    type: string
    semantic: "The employee's unique staff number"
    generator: sequence
    format: "EMP-{000000}"
    primary_key: true
    unique: true
    nullable: false
    null_probability: 0.0
    depends_on: []
    context: []
    constraints: { ... }
    examples: []
    tone: null
    locale: null
```

| Key | Meaning |
|---|---|
| `type` | One of the primitive types below. Default `string`. |
| `semantic` | What the field **means**, in natural language. Drives generator recommendation and prompt compilation. |
| `description` | Documentation. Used as a fallback for `semantic`. |
| `generator` | Which strategy produces the value. Omit it and one is recommended. |
| `unique` | Enforced by the validator; the linter rejects domains too small to satisfy it. |
| `nullable` / `null_probability` | `nullable: true` alone means 5%. |
| `depends_on` | Extra field dependencies beyond the ones the generator declares. |
| `context` | Related entities *or* sibling fields the generator may consult. |
| `primary_key` | Marks the record identifier. |
| `constraints` | See below. |
| `tone`, `locale`, `examples` | Hints for generators that can use them. |

Any key the field model does not recognise becomes a **generator option**. That
is what makes `generator: sequence` plus `format: "EMP-{000000}"` work. The
fully explicit form is also accepted:

```yaml
generator:
  type: sequence
  format: "EMP-{000000}"
```

### Types

`string` `text` `integer` `float` `decimal` `boolean` `uuid` `date` `time`
`datetime` `duration` `enum` `array` `object` `binary` `image` `audio` `file`
`uri` `ip_address` `cidr` `mac_address` `hostname` `email` `phone` `geo_point`
`json` `custom`

Types constrain what counts as a valid value. They do not determine how the
value is generated.

### Constraints

```yaml
constraints:
  min: 18
  max: 65
  min_length: 40
  max_length: 300
  pattern: "^EMP-\\d{6}$"
  enum: [active, suspended]
  forbidden: [test, TODO]
  multiple_of: 5
  precision: 2
```

`min`/`max` work on numbers, dates and strings. A date bound may be written as
an ISO string.

---

## Generators

### `constant`
`value` — always produce the same value.

### `sequence` — aliases `serial`, `autoincrement`
`format`, `start`, `step`, `pad`. Derived from the record index, so record
4,823,913 has the same id however it is reached.

```yaml
generator: sequence
format: "USER-{000000}"     # USER-000001, USER-000002, ...
```

### `uuid` — alias `guid`
`version` (4 or 5), `namespace`, `name`, `string`. Version 4 draws its bits
from the field's seed, so it is random-looking but reproducible.

### `random` — alias `rand`
`min`, `max`, `precision`, `length`, `min_length`, `max_length`, `charset`.
Behaviour follows the field's type: integers get integers, strings get
characters, temporal fields delegate to `datetime`.

`charset`: `alpha`, `alphanumeric`, `lower`, `upper`, `digits`, `hex`, or a
literal set of characters.

### `boolean` — aliases `bool`, `flag`
`probability` — a weighted coin flip.

### `weighted` — aliases `choice`, `categorical`, `enum`
`choices`, as a mapping, a plain list, or a list of `{value, weight}`.

```yaml
generator: weighted
choices:
  Windows: 67
  macOS: 18
  Linux: 13
  Other: 2
```

### `distribution` — aliases `dist`, `statistical`
`distribution`: `uniform`, `normal`, `lognormal`, `exponential`, `poisson`,
`beta`, `histogram`. Plus `mean`, `stddev`, `rate`, `scale`, `lam`, `alpha`,
`beta`, `bins`, `min`, `max`, `precision`. `min`/`max` clamp the result.

```yaml
generator: distribution
distribution: normal
mean: 38
stddev: 10
min: 21
max: 67
```

### `lookup` — aliases `dataset`, `from_list`
`values`, `path` (`.csv`, `.json`, `.txt`), `column`, `mode` (`random` or
`cycle`).

### `pattern` — aliases `shape`, `mask`
`pattern`, `upper`, `lower`.

| Token | Produces |
|---|---|
| `{A-Z}` | one character from the range |
| `{A-Z:3}` | three characters from the range |
| `{0000}` | four random digits |
| `{???}` | three random letters |
| `{***}` | three random alphanumerics |
| `{{`, `}}` | a literal brace |

```yaml
generator: pattern
pattern: "SRV-{A-Z}{A-Z}-{0000}"     # SRV-KQ-4817
```

### `template` — aliases `interpolate`, `format`
`template`, `on_missing` (`empty`, `error`, `keep`). Filters chain with `|`:
`lower`, `upper`, `title`, `strip`, `slug`, `initial`, `trunc:N`, `pad:N`,
`nospace`.

```yaml
generator: template
template: "{first_name|lower}.{last_name|lower}@example.com"
```

### `expression` — aliases `expr`, `derived`, `computed`
`expression`. Evaluated from a parsed syntax tree with an allow-list of node
types and functions — no `eval`, no imports, no attribute access beyond dotted
record lookups.

Functions: `lower` `upper` `title` `strip` `len` `str` `int` `float` `bool`
`round` `abs` `min` `max` `sum` `concat` `join` `replace` `substr` `slug`
`coalesce` `iif` `when` `hash` `year` `month` `day` `format_date` `contains`
`startswith`

```yaml
generator: expression
expression: 'lower(substr(first_name, 0, 1) + last_name)'
```

`iif(condition, a, b)` rather than `if`, because `if` is a Python keyword.

### `datetime` — aliases `date`, `time`, `timestamp`, `temporal`
`start`, `end`, `business_hours`, `weekdays_only`, `timezone_offset`. Returns
the type the field declares.

### `ip` — aliases `ip_address`, `ipv4`
`network`, `version`, `safe`. Without `network`, draws from RFC 5737/3849
documentation ranges.

### `mac` — alias `mac_address`
`oui`, `separator`, `upper`, `safe`. Without `oui`, draws from the RFC 7042
documentation range.

### `phone` — aliases `phone_number`, `telephone`
`format` (`e164`, `national`, `plain`), `area_code`, `safe`. Draws from the
555-0100–555-0199 fictitious block.

### `government_id` — aliases `ssn`, `national_id`
`masked`, `safe`. Draws from the never-issued 900 area.

### `faker` — alias `fake`
`provider`, `locale`, `safe`, `unique`. Any other option is passed to the Faker
provider. Domain-bearing values are rewritten onto reserved domains unless
`safe: false`.

### `composite` — aliases `pipeline`, `chain`
`steps` — run generators in sequence, threading the value through.

### `transform` — alias `post_process`
`operations`, `source`. Operations: `lowercase`, `uppercase`, `title`, `strip`,
`truncate:N`, `hash`, `mask:N`, `normalize`, `round:N`, `slug`.

### `null` — aliases `none`, `empty`
Always null.

### `llm` — aliases `language_model`, `ai`, `gpt`

`provider`, `mode`, `context`, `max_tokens`, `temperature`, `on_unavailable`.

No prompt. The field's `semantic`, `type`, `constraints`, `tone`, `examples`
and `context` are compiled into one by the prompt compiler; run
`cacophony prompt <project>` to read what it wrote.

```yaml
resolution_notes:
  type: text
  semantic: >
    Natural-language notes written by an IT support technician explaining
    how the ticket was resolved.
  tone: Concise internal enterprise IT helpdesk writing
  generator: llm
  provider: local_llm
  mode: per_record
  context: [category, device_type, status]
  constraints:
    min_length: 40
    max_length: 300
```

`mode` chooses how many fields and records one call covers:

| Mode | Calls for 1,000 records × 3 AI fields | When |
|---|---|---|
| `per_field` | 3,000 | Maximum control; each field gets the model's full attention |
| `per_record` *(default)* | 1,000 | Fields of one record stay mutually consistent |
| `batch` | 1,000 ÷ `--llm-batch-size` | Bulk generation; far fewer calls |
| `expansion` | same as `per_record` | Explicit name for what every mode already does |

`context` names the already-generated fields the model is shown. Omit it and
every deterministic field of the record is offered — a model given no context
will write a biography that contradicts the record it belongs to.

`on_unavailable` governs what happens when no model can answer: `error`
(default), `placeholder` (a marked stand-in, fitted to the field's length
constraints) or `null`.

### `reference` — aliases `fk`, `foreign_key`, `belongs_to`

A foreign key. `entity` (required), `field`, `distribution`, `skew`, `unique`,
`on_unavailable`.

```yaml
order:
  count: 45000
  fields:
    customer:
      generator: reference
      entity: customer          # which entity to point at
      distribution: skewed      # uniform, skewed, sequential, round_robin
      skew: 1.9
```

`field` chooses which column to point at; it defaults to the target's primary
key. A reference with no declared `type` takes the type of the key it points
at, because an integer key referenced by a string column joins to nothing.

**`distribution`** is the option that decides whether the data resembles
anything:

| Value | Behaviour |
|---|---|
| `uniform` *(default)* | Every parent equally likely |
| `skewed` | A power law: the head of the range attracts most references |
| `sequential` / `round_robin` | Parent `record_index % count` — every parent appears |

`skew` sets how steep `skewed` is. The busiest tenth of parents take
`0.1 ** (1 / skew)` of the references:

| `skew` | Share taken by the top 10% |
|---|---|
| 1.0 | 10% (uniform) |
| 1.6 *(default)* | 24% |
| 2.0 | 32% |
| 3.3 | 50% |
| 7.2 | 80% |

`unique: true` gives each parent exactly one child, and forces `sequential`.
Writing it on the field (`unique: true` beside `type:`) means the same thing as
writing it as an option.

References cost no memory. A parent is reconstructed from its index rather than
held in a pool, so a hundred million events pointing at five thousand employees
is the same amount of state as five.

**Reading a parent's other fields.** A template or expression may name the
referenced entity:

```yaml
employee:
  fields:
    employer:
      generator: reference
      entity: company
    email:
      generator: template
      template: "{first_name|lower}@{company.domain}"     # this row's company
```

The compiler orders `email` after `employer` on its own. Where a field and the
entity it points at share a name — `customer:` holding a reference to
`customer` — a dotted read means the related record, not the key.

### `image` — aliases `invokeai`, `text_to_image`

Send a constructed prompt to an image provider. `prompt`, `style`, `width`,
`height`, `steps`, `guidance`, `negative_prompt`, `workflow`, `provider`,
`reuse`, `on_unavailable`.

```yaml
portrait:
  type: image
  generator: image
  prompt: "corporate headshot of {first_name} {last_name}, {team} agent"
  width: 256
  height: 256
  style: portrait          # procedural styles: identicon, portrait, card, document
```

The prompt is a `{field}` template over this record, so the compiler orders the
image after the fields it names. Without a prompt, the field's `semantic` plus
the record's values stand in.

### `tts` — aliases `speech`, `voice`

Generate audio from generated text. `source` (required), `voice`,
`voice_field`, `speed`, `language`, `sample_rate`, `provider`, `reuse`,
`on_unavailable`.

```yaml
recording:
  type: audio
  generator: tts
  source: transcript       # the field holding the words
  voice: agent
```

The clip's duration and an aligned transcript are recorded in the asset
manifest, which is what makes a generated speech set usable.

### `document` — aliases `pdf`, `invoice`, `report`

Render a record as a document. `template` or `template_path` (one required),
`format` (`pdf`, `html`, `txt`), `title`, `page_size`, `font`, `font_size`.

```yaml
id_badge:
  type: file
  generator: document
  format: pdf
  page_size: a5
  title: "{first_name} {last_name} - {employee_id}"
  template: |
    {first_name} {last_name}
    {employee_id}
    Team: {team}
```

Needs no provider: a document is rendered from the record it describes.
Templates take `{field}` and `{related.field}` and nothing else — deliberately
not a template *language*, since a project file is something people share.

### Declared, not yet implemented

`script` — see [ROADMAP.md](ROADMAP.md). Accepts `on_unavailable`: `error`
(default), `placeholder`, `null`.

---

## Assets

A field that writes a file puts the path in the record and the bytes under
`<out-dir>/assets/`:

```text
out/
├── employee.jsonl
└── assets/
    ├── manifest.jsonl
    └── employee/00000000/employee_00000000_portrait.png
```

Paths are derived from entity, record index and field, so a resumed run reuses
what it already produced rather than paying for it again. Identical bytes are
stored once. `manifest.jsonl` records one line per asset — the record it
belongs to, its media type, its size, and the provenance of section 19:

```json
{"entity": "employee", "record_index": 0, "field": "portrait", "kind": "image",
 "media_type": "image/png", "size_bytes": 2588, "record_id": "SUP-0001",
 "metadata": {"provider": "pictures", "workflow": "procedural:portrait",
              "seed": 1585453150, "prompt_hash": "514e7bbc1af5d2bb"}}
```

`--assets-dir` puts them elsewhere; `--regenerate-assets` redraws what is
already there.

---

## `relationships`

```yaml
relationships:
  - from: company
    to: employee
    cardinality: one_to_many    # one_to_one, one_to_many, many_to_one, many_to_many
    field: company_id
    required: true
```

Relationships affect entity ordering. They are documentation rather than
machinery: what actually creates a foreign key is a field with
`generator: reference`, and a `relationships:` block is not needed for that.

## `providers`

```yaml
providers:
  local_llm:
    type: language_model        # language_model, image, speech, custom
    adapter: ollama
    base_url: http://localhost:11434
    model: llama3.1:8b
    secret: my-secret-id        # a logical id, never a credential
    concurrency: 4
    timeout_seconds: 120
    options:                    # adapter-specific
      structured_output: auto
```

| Adapter | Aliases | Endpoint | Structured output |
|---|---|---|---|
| `ollama` | – | `/api/generate` | JSON Schema, enforced during decoding |
| `llamacpp` | `llama.cpp`, `llama_cpp` | `/completion` | JSON Schema, via GBNF grammar |
| `openai_compatible` | `openai`, `vllm`, `lmstudio`, `tgi` | `/v1/chat/completions` | Negotiated: `json_schema`, then `json_object`, then prompt-only |
| `mock` | `fake`, `mock_llm` | none — in process | Synthesised from the schema |

`openai_compatible` accepts `structured_output`: `auto` (default),
`json_schema`, `json_object` or `none`. `auto` discovers what the server
supports on the first call and remembers the answer.

`mock` accepts `failure_rate`, `malformed_rate`, `latency_ms`, `responses` and
`healthy` — useful for rehearsing a run's shape, and for exercising the retry
ladder deliberately.

### Image and speech adapters

| Adapter | Aliases | Type | Notes |
|---|---|---|---|
| `invokeai` | `invoke` | image | Submits a workflow graph and polls the queue. `queue_id`, `scheduler`, `steps`, `guidance`, `negative_prompt`, `poll_timeout_seconds` |
| `procedural_image` | `procedural`, `placeholder_image` | image | Draws in-process. `style`: identicon, portrait, card, document |
| `piper` | `piper_http` | speech | POST text, receive WAV |
| `openai_speech` | `openai_tts`, `speech_api` | speech | `POST /v1/audio/speech` — openedai-speech, LocalAI, Kokoro-FastAPI |
| `procedural_speech` | `tone`, `placeholder_speech` | speech | Synthesises in-process. `sample_rate`, `voice` |

The two procedural adapters need no GPU and no server. They produce
deterministic, obviously-synthetic media so a multimodal schema can be
designed, previewed and tested anywhere; every result is labelled
`synthetic: true`. Swapping in `invokeai` or `piper` changes one line.

Credentials never appear in a project file. `secret` names an entry resolved at
run time from the environment variable `CACOPHONY_SECRET_<ID>`, from a variable
named after the id itself, or from the OS keychain under service `cacophony`.
The loader rejects anything that looks like a literal key.

## `scenarios`

```yaml
scenarios:
  ransomware:
    description: Sign-in, phishing, execution, lateral movement, encryption.
    applies_to: [user, device, authentication]
    affects_fraction: 0.0002
    enabled: false
```

Recorded in the schema and version-controlled with it. Executed from Phase 7.

## `chaos`

```yaml
chaos:
  preset: realistic     # pristine, realistic, messy, hostile_qa, absolute
  outliers: 0.05
  missing_data: 0.02
  duplicates: 0.01
  malformed_text: 0.01
  unexpected_unicode: 0.005
  temporal_anomalies: 0.001
  referential_anomalies: 0.0
```

Values are fractions of records affected. The knobs are part of the schema
contract now; the injectors arrive with Phase 3.

## `outputs`

```yaml
outputs:
  analytics:
    format: parquet
    path: out/analytics
    partition_by: [year, month, day]
  fixtures:
    format: jsonl
    path: out/fixtures
```

Formats: `csv`, `json`, `jsonl` / `ndjson`, `parquet`, `sqlite`, `sql`.

`sqlite` writes one database for the whole project, with every entity as a
table and every reference as an enforced `FOREIGN KEY`. `sql` writes a portable
`CREATE TABLE` plus `INSERT` script per entity, for a database Cacophony has no
adapter for.

```bash
cacophony generate project.yaml -o sqlite -d out/
sqlite3 out/retail-commerce.db "PRAGMA foreign_key_check"   # silent
```

---

## Commands

```bash
cacophony validate  project.yaml [--seed N]
cacophony lint      project.yaml [--strict]
cacophony plan      project.yaml [--seed N] [--json]
cacophony preview   project.yaml [-e ENTITY] [-n N] [-c a,b,c] [--offset N] [--json] [--isolate]
cacophony generate  project.yaml [-n N] [--seed N] [-o FORMAT] [-d DIR] [-e ENTITY]
                                 [--batch-size N] [--provenance MODE]
                                 [--on-failure POLICY] [--drop-invalid] [--no-validate]
                                 [--assets-dir DIR] [--regenerate-assets]
cacophony generators [--json]
cacophony providers  [project.yaml] [--test]
cacophony models     project.yaml [-p PROVIDER]
cacophony prompt     project.yaml [-e ENTITY] [--batch-size N] [--schema]
cacophony propose    "a description" [--out FILE] [--providers project.yaml]
                                     [--adapter NAME] [--url URL] [-m MODEL]
                                     [--scale N] [--seed N] [--force]

cacophony runs       [-p PROJECT] [--store FILE] [--state STATE] [--json]
cacophony run        RUN_ID [-p PROJECT] [--events N] [--json]
cacophony resume     [RUN_ID] [-p PROJECT] [--store FILE]
cacophony serve      [--host H] [--port P] [--store FILE]
cacophony version
```

`generate` and `preview` also accept `--cache MODE` (`disabled`, `read_only`,
`read_write`), `--cache-path FILE` and `--llm-batch-size N`.

`generate` additionally accepts `--workers N` (entities generated
concurrently), `--checkpoint-every N`, `--store FILE`, `--no-history`,
`--log-level LEVEL` and `--log-format text|json`.

Exit code `4` means a run was cancelled rather than failed.

---

## Runs

Every `generate` records a run in a SQLite store, by default
`.cacophony/cacophony.db` beside the schema file. The store holds projects,
schema revisions, runs, jobs, checkpoints, events and statistics — never the
generated data itself.

```bash
cacophony generate project.yaml -o parquet -d out/   # ^C at any point
cacophony runs                                       # what has been run
cacophony run 4f2a91c3 --events 20                   # the inspector
cacophony resume 4f2a91c3                            # carry on
```

A job checkpoints after every batch. On resume the file on disk is counted and
the checkpoint corrected to match, so an unclean stop cannot leave duplicated
or skipped records. Formats that cannot be appended to — JSON arrays, Parquet —
resume into a new part file (`employee.part0001.parquet`); readers for both
accept a directory of parts.

A resumed run continues under the schema revision it started with, and reuses
its original configuration. Editing a schema and resuming would produce a
dataset generated two different ways; start a new run instead.

`--no-history` skips the store entirely.

---

## The API

```bash
pip install 'cacophony[api]'
cacophony serve --port 8765
```

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/projects` | List registered projects |
| `POST` | `/api/projects` | Register one, by `path` or inline `source` |
| `GET` | `/api/projects/{id}` | Project with its schema revisions |
| `GET` | `/api/projects/{id}/plan` | The compiled generation plan |
| `GET` | `/api/projects/{id}/lint` | Linter findings |
| `POST` | `/api/projects/{id}/preview` | Sample records, with column sources |
| `POST` | `/api/projects/{id}/runs` | Start a run |
| `GET` | `/api/runs` | List runs, filterable by state |
| `GET` | `/api/runs/{id}` | A run, plus live metrics while it executes |
| `GET` | `/api/runs/{id}/jobs` | Jobs and their checkpoints |
| `GET` | `/api/runs/{id}/events` | Structured event log |
| `POST` | `/api/runs/{id}/pause` | Pause at the next batch boundary |
| `POST` | `/api/runs/{id}/resume` | Unpause, or restart from checkpoints |
| `POST` | `/api/runs/{id}/cancel` | Cancel, checkpointing on the way out |
| `GET` | `/api/runs/{id}/quality` | Referential and distribution scores |
| `GET` | `/api/runs/{id}/assets` | Generated files, filterable by kind and entity |
| `GET` | `/api/runs/{id}/assets/file` | One file, refusing paths outside the run |
| `DELETE` | `/api/runs/{id}` | Delete a finished run |
| `WS` | `/api/runs/{id}/stream` | Live progress |
| `GET` | `/api/providers` | Adapters, and a project's configured providers |
| `GET` | `/api/providers/{id}/models` | Models a provider serves |
| `POST` | `/api/providers/{id}/test` | Health check |
| `GET` | `/api/generators` | The generator registry |
| `GET` | `/api/system` | Version and store statistics |

### The Schema Studio

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/projects/{id}/schema` | Source text plus the compiled shape |
| `PATCH` | `/api/projects/{id}/schema` | Targeted edits, preserving the document |
| `PUT` | `/api/projects/{id}/schema` | Replace the whole document |
| `GET` | `/api/schema/types` | Types and generators, for the editor controls |
| `GET` | `/api/schema/operations` | The edit operations and their arguments |

A `PATCH` body is a list of operations applied as one transaction:

```json
{
  "operations": [
    {"op": "set_entity", "entity": "employee", "key": "count", "value": 10000},
    {"op": "set_field", "entity": "employee", "field": "biography",
     "key": "semantic", "value": "A short professional biography."}
  ]
}
```

| Operation | Arguments |
|---|---|
| `set_project` | `key`, `value` |
| `set_entity` | `entity`, `key`, `value` |
| `add_entity` / `remove_entity` | `name` |
| `set_field` / `unset_field` | `entity`, `field`, `key`, `value` |
| `add_field` | `entity`, `name`, optional `value`, `index` |
| `remove_field` | `entity`, `name` |
| `rename_field` | `entity`, `field`, `name` |
| `move_field` | `entity`, `name`, `index` |

A `value` of `null` removes the key rather than writing `null`, because that is
what clearing a control in a form means.

Edits are applied to the YAML document in place, so comments, ordering and
formatting survive. The result must load and compile or the whole patch is
refused and the file is untouched. Every accepted patch creates a schema
revision (section 73).

Interactive documentation is at `/docs`.

Exit codes: `0` success, `1` lint errors, `2` bad schema or bad arguments,
`3` generation failure. Errors go to stderr, so `cacophony preview --json | jq`
is safe.

`--on-failure`: `abort` (default), `retry`, `skip`, `placeholder`, `incomplete`.
