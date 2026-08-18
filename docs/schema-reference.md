# Schema reference

The complete project schema. See [CACOPHONY.md](../CACOPHONY.md) for the design
rationale, [ROADMAP.md](ROADMAP.md) for what each delivery phase built, and
[Cacophony-Manual.pdf](Cacophony-Manual.pdf) for the same material at length.

A project is one YAML or JSON file with up to twelve top-level keys:

```yaml
project:        # metadata, seed, locale, provenance mode
entities:       # the record types to generate
relationships:  # documentation of connections between entities
providers:      # generation backends: models, image, speech
timeline:       # when this project's events happen
scenarios:      # behavioural overlays
chaos:          # entropy injection
quality:        # duplication thresholds worth measuring
recipes:        # this project's own reusable fragments
requires:       # what the project needs that it does not carry
patches:        # edits recorded as rules
outputs:        # named output profiles
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
| `profile` | enum | `balanced` | `quick_mock`, `balanced`, `high_realism`, `maximum_chaos` — see below |
| `provenance` | enum | `none` | `none`, `run`, `record`, `field`, `full` |

**What `profile` actually does**, section 77 being ambitious and this being the
part of it that is built:

| Profile | Effect |
|---|---|
| `balanced` *(default)* | Nothing. The baseline. |
| `quick_mock` | Asks a language model for short, plain answers. |
| `high_realism` | Asks a language model for specific, plausible detail. |
| `maximum_chaos` | Turns on the `hostile_qa` chaos preset and 5% edge cases, unless the project or the command line says otherwise. |

The first three touch only the prompts a model is given; they do not change
which generators are chosen or how much entropy is injected. `maximum_chaos` is
the one that changes the data, and anything you state explicitly still wins over
it.

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
    tags: [core]              # free labels, shown by `cacophony plan`
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

### `event_time` — aliases `occurred_at`, `timeline`

When an event happened (section 25). `jitter`, `offset`, `offset_field`,
`spread` (`ordered` or `random`). Needs the project `timeline:` and the
entity's `simulation:`.

`ordered` places the *k*-th of a subject's *n* events where the *k*-th event
would fall, so a subject's events come out chronological without being sorted.

### `subject` — aliases `actor`, `belongs_to_subject`

The subject an event belongs to (section 25). `field` chooses which column of
the subject to point at; it defaults to the primary key and adopts its type.

Unlike `reference`, this does not *choose* a parent — the allocation already
decided whose event this is, which is what makes a subject's events consecutive
and countable.

### `state` — aliases `running`, `accumulated`

A value folded over this subject's earlier events (section 26). `variable`
names which one; it defaults to the field's own name.

### `scenario` — aliases `incident`, `affected_by`

What a scenario is doing to this record (section 17). `report`: `name`
(default), `phase`, `involved` or `position`. `normal` is the value for a
record no scenario touches.

### Declared, not yet implemented

`script` — see [ROADMAP.md](ROADMAP.md). Accepts `on_unavailable`: `error`
(default), `placeholder`, `null`.

---

## `timeline`

When a project's events happen (section 25).

```yaml
timeline:
  start: "2026-01-01"
  end: "2026-04-01"
  shape: business_hours     # flat, business_hours, office, retail, evening
  holidays: ["2026-01-01", "2026-02-16"]
  holiday_weight: 0.05      # 0.0 silences a holiday entirely
  months: {december: 2.5}   # seasonality, by name or number
  spikes:
    - {start: "2026-03-01", end: "2026-03-07", multiplier: 8}
  growth: 1.4               # activity at the end relative to the start
```

## `simulation`

Declared on an entity, it turns its records into a history (sections 25, 26).

```yaml
transaction:
  count: 500000
  simulation:
    subject: account        # whose events these are
    distribution: skewed    # uniform, skewed, zipf
    skew: 1.9               # as for a reference: top 10% take 0.1 ** (1/skew)
    minimum: 4              # events every subject gets before the rest
    state:
      balance:
        initial: "500"
        update: "balance + amount"
        min: 0
        precision: 2
```

Events are laid out in contiguous blocks per subject, so a state fold is linear
and a resumed run replays one subject's block rather than the dataset. State
expressions use the same restricted evaluator as `expression`: no imports, no
attribute access, no way to run code from a shared project file.

## `scenarios`

A reusable behavioural pattern applied to a fraction of subjects (section 17).

```yaml
scenarios:
  ransomware:
    description: Access, then execution, then encryption.
    applies_to: [authentication]
    affects_fraction: 0.004
    parameters:
      subject: user
      window: {at: 0.62, duration: 0.12}   # or [start, end]
      rate_multiplier: 4.0
      effects:
        risk_score: 88
      phases:
        - name: initial_access
          effects:
            result: {success: 70, failure_bad_password: 30}
        - name: encryption
          effects:
            application: Admin Console
            risk_score: 99
```

Subjects are selected by hashing the project seed and the scenario's name, so
the same identities are affected on every run, in any order, at any scale, and
no list is ever held. A mapping effect is a weighted choice; a string beginning
`=` is an expression over the record.

`window` is a fraction of the subject's own history — which, because events are
laid out by timeline quantile, is the same calendar moment for every affected
subject however many events each has.

## `chaos`

Deliberate damage (sections 24, 78).

```yaml
chaos:
  preset: realistic     # pristine, realistic, messy, hostile_qa, absolute
  missing_data: 0.05    # anything stated explicitly overrides the preset
```

| Kind | What it does |
|---|---|
| `missing_data` | Nulls a field that had a value |
| `malformed_text` | Whitespace, case, transpositions, truncation, tabs |
| `unexpected_unicode` | Zero-width spaces, RTL marks, combining accents, emoji |
| `outliers` | Numbers orders of magnitude out, sometimes negative |
| `temporal_anomalies` | Dates in the far future or past, or in another format |
| `referential_anomalies` | A well-formed key that points at nothing |
| `duplicates` | Emits the record twice |

Damage is recorded in `_chaos` on the record and in provenance, and the
validator skips damaged fields — otherwise a chaotic run is a wall of
validation failures - which would now *stop* the run - and `--drop-invalid`
discards exactly what was asked for.
Primary keys and `scenario` fields are never damaged.

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
| `invokeai` | `invoke` | image | Submits a graph and polls the queue. `queue_id`, `scheduler`, `steps`, `guidance`, `negative_prompt`, `poll_timeout_seconds` |
| `procedural_image` | `procedural`, `placeholder_image` | image | Draws in-process. `style`: identicon, portrait, card, document |
| `piper` | `piper_http` | speech | `POST /synthesize` — piper1-gpl's web server |
| `openai_speech` | `openai_tts`, `speech_api` | speech | `POST /v1/audio/speech` — openedai-speech, LocalAI, Kokoro-FastAPI |
| `procedural_speech` | `tone`, `placeholder_speech` | speech | Synthesises in-process. `sample_rate`, `voice` |

```yaml
providers:
  pictures:
    type: image
    adapter: invokeai
    base_url: http://diffusion-box:9090
    model: Dreamshaper 8       # a name from the server's model list, or a key
    concurrency: 1             # one GPU, one request
    options:
      steps: 24
      guidance: 7.5
      poll_timeout_seconds: 300

  voices:
    type: speech
    adapter: piper
    base_url: http://tts-box:5000
```

`invokeai` builds a text-to-image graph for the named model's architecture:
SD-1, SD-2 and SDXL are covered. FLUX, Qwen-Image and Z-Image each have their
own node topology, so those need a `workflow` exported from InvokeAI — the
adapter says so by name rather than submitting a graph that will fail. Naming
no model picks an installed one the built-in graph can wire.

A Piper server hosts one voice, chosen when it starts, so a schema that wants
several voices points each at its own provider.

Both adapters are verified against real servers by the live contract tests:

```bash
CACOPHONY_TEST_INVOKEAI=http://diffusion-box:9090 \
CACOPHONY_TEST_PIPER=http://tts-box:5000 \
CACOPHONY_TEST_OLLAMA=http://gpu-box:11434 \
    pytest tests/test_provider_contracts.py -m live
```

The two procedural adapters need no GPU and no server. They produce
deterministic, obviously-synthetic media so a multimodal schema can be
designed, previewed and tested anywhere; every result is labelled
`synthetic: true`. Swapping in `invokeai` or `piper` changes one line.

Credentials never appear in a project file. `secret` names an entry resolved at
run time from the environment variable `CACOPHONY_SECRET_<ID>`, from a variable
named after the id itself, or from the OS keychain under service `cacophony`.
The loader rejects anything that looks like a literal key.

## `recipes`

Design document sections 80, 106. A reusable fragment of schema.

```yaml
entities:
  employee:
    count: 5000
    recipes: [employee]        # twelve fields, one line
    fields:
      email:                   # override one without restating the rest
        template: "{first_name|lower}.{last_name|lower}@acme.example"
      cost_centre:             # and add your own
        type: string
        generator: pattern
        pattern: "CC-{0000}"
```

```bash
cacophony recipes                    # every recipe, by group
cacophony recipes --show employee    # its fields, and where each came from
```

**Expansion is visible.** Every expanded field records the recipe it came from
in `recipe:`, `cacophony plan` prints `via employee` beside it, and the Studio
badges it in the field editor. A schema that silently gains eight fields is a
schema nobody can debug.

**Overriding does not require forking.** Naming a field the recipe defines
overrides it key by key, *in the place the recipe put it*, so the record does
not reorder. Naming a different `generator` replaces the generator and its
options wholesale — merging `generator: llm` onto `generator: template` would
leave the template's options behind as junk.

`$self` is the only substitution: it becomes the name of the entity being
expanded into, which is what section 80's manager relationship needs.

### Where recipes come from

In increasing precedence: the built-in catalogue, a `recipes/` directory beside
the project file, then the project's own `recipes:` block. A project may
therefore replace a built-in by defining one with the same name.

```yaml
recipes:
  cost_centre:
    description: Our own coding.
    fields:
      cost_centre: {type: string, generator: pattern, pattern: "CC-{0000}"}
```

A recipe may `includes:` others; cycles are refused by name.

### The built-in catalogue

Section 106's five groups, 31 recipes. Every one compiles, generates and passes
its own validation — asserted per recipe in the test suite, because a catalogue
is a promise and a recipe that does not work is worse than no recipe.

| Group | Recipes |
|---|---|
| `identity` | `name` `person` `employee` `customer` `username` `email` `address` |
| `computing` | `hostname` `ip` `mac` `os` `browser` `device` `software_version` |
| `security` | `cvss_score` `cve_identifier` `alert_severity` `logon_event` `network_event` `hash_value` |
| `commerce` | `product` `sku` `transaction` `invoice` `price` `currency` |
| `operational` | `ticket` `status` `priority` `comment` `timestamp` |

Everything is deterministic except `comment` and `ticket`, whose prose fields
use a language model and carry `on_unavailable: placeholder` so a project with
no model server still runs.

Section 62 holds throughout: addresses come from RFC 5737/3849 documentation
ranges, MAC addresses from RFC 7042's OUI, email from RFC 2606 reserved domains,
telephone numbers from the 555-01xx fiction block. CVE identifiers are shaped
like real ones and use a year with none published, so nothing here can be
mistaken for threat intelligence.

### Paths are relative to the schema

A relative `path:` in a project or recipe resolves against the **schema file**,
not the working directory. Without that, a project only works from the directory
it lives in, and a portable bundle is impossible — the only paths that would
work are absolute ones, which are exactly the paths that cannot travel.

---

## `quality`

Design document sections 58, 59.

```yaml
quality:
  duplication:
    max_exact: 0.001        # fraction of compared *values*, not records
    max_near: 0.02
    fields: [biography]     # default: the long-form text a model wrote
    methods: [exact, normalized, minhash]
    similarity: 0.7         # Jaccard at which two texts are the same thing
    shingle: 3              # word n-gram width
    window: 50000           # recent values held for near-duplicate comparison
    error_rate: 0.001       # Bloom filter false-positive target
```

Writing a threshold is a request to measure; nothing is checked that nobody
asked for. `enabled: false` overrides that.

| Method | Catches |
|---|---|
| `exact` | Byte-identical values |
| `normalized` | The same text casefolded, depunctuated, whitespace-collapsed |
| `minhash` | Shared *phrasing* — a paragraph rewritten around one clause |
| `fuzzy` | The same candidates, confirmed with a real sequence ratio |
| `embeddings` | Named in section 59 and **refused**: it needs an embedding provider, and no adapter offers one |

`fields` defaults to model-written fields, declared `text`, and strings whose
`max_length` is 80 or more. Comparing every employee id against every other one
finds nothing and costs a great deal; reporting that a weighted choice recurs
would be reporting what a weighted choice is for. `["*"]` compares whole
records.

**What to believe.** Exact matching uses a Bloom filter — 18 MB for ten million
values rather than most of a gigabyte. It has no false negatives, so a report of
zero is exact, and its false-positive rate at the load it actually reached is
reported beside the count. Near-duplicate detection holds a sliding window,
because model repetition is *local*: the same three biographies come back within
a few hundred calls, and a window catches that at any dataset size where a
uniform sample would not.

Deliberate duplicates from `chaos` are excluded. Reporting those would be
reporting the feature.

The defaults are calibrated rather than guessed. On a sixty-word biography with
only the name changed — the canonical way a model repeats itself — word trigram
Jaccard is 0.82; with a clause rewritten too it is 0.69; two biographies sharing
an opening sentence and nothing else score 0.13.

---

## `requires`

Design document section 44. What a project needs that it does not carry.

```yaml
requires:
  plugins: [network_packets]
```

Checked at compile time, so a project whose plugin is not installed fails
immediately and by name rather than three million records into a run with a
field quietly missing. `cacophony plugins` lists what is installed.

---

## `patches`

Design document sections 104, 105. An edit recorded as a rule.

```yaml
patches:
  mask_finance_emails:
    description: Finance addresses are masked before this dataset leaves the building.
    entity: employee                        # omit to apply to every entity
    where: "department == 'Finance'"        # omit to apply to every record
    set:
      email: "mask:8"                      # an operation pipeline
      notes: "upper(department)"            # or an expression over the record
      internal_ref: {value: null}           # or a literal
  drop_test_accounts:
    where: "startswith(email, 'test@')"
    drop: true                              # or `keep: true` for the inverse
```

**A rule, not a mutation — and that is the whole point.** A Cacophony dataset is
a pure function of its schema and its seed. Editing a row in an output file
breaks that silently: the file stops corresponding to anything, no other machine
reproduces it, and the next `generate` overwrites the edit without noticing. A
rule in the project applies on every run, travels with the schema, and
`cacophony regenerate` produces the edited value a year later.

Rules apply in authored order, after chaos and before validation — so what the
validator checks is what reaches the file, and a rule is the last word on what a
record contains. Order matters: masking then hashing is a different value from
hashing then masking.

### Operations

Section 105's list, available in `patches:`, in the `transform` generator, and
on the command line. Several are joined with `|`.

| Operation | Effect |
|---|---|
| `lowercase` `uppercase` `title` `strip` | Case and whitespace |
| `truncate:N` | At most N characters (default 50) |
| `normalize` | Collapse whitespace, NFKC |
| `slug` | Lowercase, hyphenated |
| `mask:N` | All but the last N characters become `*` (default 4) |
| `hash:N` | A stable BLAKE2b digest, N hex characters (default 32) |
| `round:N` | N decimal places; a `Decimal` stays a `Decimal` |
| `add_noise:N` | Jitter a number by up to N per cent |
| `format_date:FMT` | `strftime` a date or datetime (default `%Y-%m-%d`) |
| `encode:KIND` | `base64`, `hex`, `url` or `json` |
| `compress:N` / `decompress` | Deflate to base64, and back |
| `nullify` | Drop the value |

**Every operation is deterministic, including `add_noise`.** Its jitter is
derived by hashing the value, not drawn from a generator — because an operation
that reached for a random number would make a transformed dataset
unreproducible, and would change the file every time it ran. The same value
always moves the same way.

**An inapplicable operation raises.** Rounding a name or formatting something
that is not a date stops the pass rather than passing the value through: a
masking run that quietly skipped half a column is worse than one that failed.
A null short-circuits, because deliberate nulls are what a chaos preset put
there.

---

## `outputs`

```yaml
outputs:
  analytics:
    format: parquet
    path: out/analytics
    partition_by: [region, opened_on]
    options: {compression: zstd}
  fixtures:
    format: csv
    path: out/fixtures
    entities: [employee]
```

```bash
cacophony generate project.yaml --output-profile analytics
cacophony generate project.yaml --output-profile fixtures
```

A profile is a named set of the options `generate` would otherwise take on the
command line: `format`, `path`, `entities`, `partition_by` and format-specific
`options`. Naming one selects it; an explicit flag still wins, so
`--output-profile analytics -d /tmp/scratch` writes the analytics layout
somewhere else. Two profiles means running the command twice, which is also what
"the same logical dataset, written two ways" means in practice.

A profile's relative `path:` resolves against the **schema file**, like every
other path in a project. `--out-dir` is a command-line argument and stays
relative to where you are standing.

Formats: `csv`, `json`, `jsonl` / `ndjson`, `parquet`, `sqlite`, `sql`.

### `partition_by`

Columns whose values become directories, which is the layout every columnar
reader already understands:

```text
out/analytics/employee/region=emea/opened_on=2026-03-01/employee.parquet
```

One file per distinct combination, opened when the first record needing it
arrives. Partitioning on a high-cardinality column is how one dataset becomes a
million tiny files, so a run stops at 512 open partitions and says so;
`options: {max_partitions: N}` raises it.

Whether the partition columns *also* stay inside the files depends on what the
format's readers do with them:

- **Parquet** reconstructs them from the directory names, and refuses a
  directory where they appear in both — *"Field region has incompatible types:
  string vs dictionary"* — so they are dropped from the files.
- **JSON Lines, CSV and JSON** have no such convention. Nothing would put the
  column back, so it is kept.

`options: {drop_partition_columns: false}` overrides either way. `sqlite` and
`sql` cannot be partitioned and say so: there is one destination for the whole
project, so there is nothing to divide.

`sqlite` writes one database for the whole project, with every entity as a
table and every reference as an enforced `FOREIGN KEY`. `sql` writes a portable
`CREATE TABLE` plus `INSERT` script per entity, for a database Cacophony has no
adapter for.

```bash
cacophony generate project.yaml -o sqlite -d out/
sqlite3 out/retail-commerce.db "PRAGMA foreign_key_check"   # silent
```

**Chaos and a database schema cannot both be had.** Entropy injection (section
24) nulls required fields, mangles keys and re-emits whole records the way a
retried insert does — every one of which a declared constraint would reject,
aborting the run on its first damaged row. A run with `chaos` enabled therefore
writes its tables without primary keys, uniqueness, `NOT NULL` or
`FOREIGN KEY`, creates indexes on the key and reference columns instead so the
joins still work, and says so when it starts. Generate without chaos for a
database whose constraints hold; generate with it for a corrupted one to test a
loader against.

---

## Commands

```bash
cacophony validate  project.yaml [--seed N]
cacophony lint      project.yaml [--strict]
cacophony plan      project.yaml [--seed N] [--json]
cacophony preview   project.yaml [-e ENTITY] [-n N] [-c a,b,c] [--offset N] [--json] [--isolate]
cacophony generate  project.yaml [-n N | -n ENTITY=N] [--seed N] [-o FORMAT] [-d DIR]
                                 [--output-profile NAME] [-e ENTITY]
                                 [--batch-size N] [--provenance MODE]
                                 [--on-failure POLICY] [--drop-invalid] [--no-validate]
                                 [--assets-dir DIR] [--regenerate-assets]
                                 [--world NAME]
cacophony generators [--json]
cacophony providers  [project.yaml] [--test]
cacophony models     project.yaml [-p PROVIDER]
cacophony prompt     project.yaml [-e ENTITY] [--batch-size N] [--schema]
cacophony worlds     project.yaml [--create NAME] [--show NAME] [--delete NAME]
cacophony benchmark  project.yaml -m MODEL,MODEL [-e ENTITY] [-n N] [-p PROVIDER]
                                  [--sort-by FIELD] [--json] [--seed N]
cacophony stream     project.yaml [-r ENTITY=RATE]... [-t DESTINATION]...
                                  [-s SECONDS] [-n RECORDS] [--from N]
                                  [--batch-size N] [--flush SECONDS]
                                  [--follow-shape] [--historical] [--validate]
                                  [--scenario-cycle SECONDS] [--on-error POLICY]
cacophony cluster    project.yaml [-o DIR] [-w WORKERS] [-f FORMAT] [-n N]
                                  [--shard-size N] [--batch-size N]
                                  [--join/--no-join] [--assets-dir DIR] [--seed N]
cacophony controller project.yaml [--host H] [--port P] [--shard-size N]
                                  [--lease-seconds S] [--max-attempts N] [-n N]
cacophony worker     project.yaml -c URL [-o DIR] [-f FORMAT] [--name ID]
                                  [--capabilities LIST] [--concurrency N]
                                  [-n N] [--assets-dir DIR] [--idle-timeout S]
cacophony propose    "a description" [--out FILE] [--providers project.yaml]
                                     [--adapter NAME] [--url URL] [-m MODEL]
                                     [--scale N] [--seed N] [--force]

cacophony runs       [-p PROJECT] [--store FILE] [--state STATE] [--json]
cacophony run        RUN_ID [-p PROJECT] [--events N] [--json]
cacophony resume     [RUN_ID] [-p PROJECT] [--store FILE]
cacophony serve      [--host H] [--port P] [--store FILE]
cacophony version

cacophony recipes    [-p project.yaml] [--show NAME] [--group NAME] [--json]
cacophony bundle export  project.yaml [-o FILE] [--force]
cacophony bundle inspect FILE [--json]
cacophony bundle import  FILE -d DIR [--force]

cacophony transform  FILE [-s 'FIELD=OP']... [-w EXPR] [--drop-where EXPR]
                          [--keep-where EXPR] [-o FILE | --in-place]
                          [-f FORMAT] [-p project.yaml] [-e ENTITY]
                          [--record-as NAME] [--force] [--json]
cacophony regenerate project.yaml -r N|N-M [-e ENTITY] [-c a,b,c] [--json] [--seed N]

cacophony plugins    [--show NAME] [--json]
cacophony desktop    [--store FILE] [-p project.yaml] [--studio DIR] [--port N]
                     [--no-token] [--keep-running]
```

`generate` and `preview` also accept `--cache MODE` (`disabled`, `read_only`,
`read_write`), `--cache-path FILE` and `--llm-batch-size N`.

`generate` additionally accepts `--workers N` (entities generated
concurrently), `--checkpoint-every N`, `--store FILE`, `--no-history`,
`--log-level LEVEL` and `--log-format text|json`.

`generate` also accepts `--edge-cases FRACTION` and
`--edge-categories a,b,c` — see [Edge cases](#edge-cases) below.

Exit code `4` means a run was cancelled rather than failed.

---

## Streaming

`cacophony stream` turns a project into a workload generator (section 35): a
rate per entity, one or more destinations, and no end.

```bash
cacophony stream templates/security-operations.yaml \
    --rate authentication=250/s --rate security_finding=8/minute \
    --to syslog://siem.internal:514
```

Rates are written the way people say them: `250/s`, `8 per minute`,
`1200/hour`, or a bare number meaning per second.

| Destination | `--to` | Notes |
|---|---|---|
| Standard output | `stdout` | One JSON object per line. The dashboard moves to stderr, so piping to `jq` works |
| File | `file://path.jsonl` | Rotates by size; `rotate_bytes` and `keep` are options |
| Syslog | `syslog://host:514`, `syslog+tcp://host:601` | RFC 5424 by default, `rfc: 3164` available. TCP uses octet-counted framing |
| HTTP | `https://host/ingest` | POSTs an ndjson batch per delivery; `array` and `single` bodies available |
| Kafka | `kafka://broker:9092/topic` | Needs `pip install 'cacophony[kafka]'`. `key_field` partitions by a field |
| Memory | `memory://200` | A bounded window of recent records, for the API and the Studio to read back. Useless from the CLI, where stdout already does this |

Several `--to` flags send the same records to every destination.

**What differs from a batch run.** Indices keep going rather than stopping at a
count, so `--from N` continues a previous stream instead of replaying it — the
summary prints the number to use. Timestamps come from the wall clock;
`--historical` keeps the generated ones, and `--follow-shape` reuses the
timeline's shape as a rate multiplier so the stream is quiet at night. Subjects
interleave, because that is what a stream is, so per-subject state is held in
memory rather than folded over a contiguous block.

Scenario windows recur over `--scenario-cycle` seconds (default one hour): an
incident declared at `window: {at: 0.62}` has no meaning in an endless stream,
so it happens again each cycle.

**Validation is off unless you ask for it.** A workload generator that stopped
mid-load because one record in a million was invalid would have failed the test
it was running, so `stream` does not check by default. `--validate` turns the
checks on; failures are counted and reported in the summary, and the records are
delivered anyway. `--on-failure` has no effect here, deliberately.

**Attainment.** The dashboard shows the achieved rate against the requested one.
Below 95% means generation or a destination could not keep up, which is
reported rather than hidden — a workload generator that quietly under-delivers
is measuring the wrong thing.

---

## Edge cases

Section 79. A QA mode that deliberately seeks weird *values*.

```bash
cacophony generate project.yaml --edge-cases 0.05
cacophony generate project.yaml --edge-cases 0.2 --edge-categories emoji,rtl_text
```

**This is not chaos, and the difference is the point.** Entropy injection
produces data the schema *forbids* — a null in a required column, a mangled
date — and answers "what does my pipeline do with broken input". Edge cases
produce data the schema *permits* and naive code mishandles anyway.
`O'Brien-Smith` is a real surname; an application that cannot store it has a
bug, not bad input.

Every value is validated against the field that will hold it, and a candidate
that does not fit is discarded and counted. An edge case that fails validation
has told you nothing about your application.

| Category | Values |
|---|---|
| `boundary_length` | Empty string where legal, exactly `min_length`, exactly `max_length` |
| `punctuation_names` | `O'Brien-Smith`, `d'Arcy`, `van der Waals`, `Ó Séaghdha` |
| `unicode_text` | One-character names, `ß`, Turkish dotted/dotless I, ligatures, fullwidth, Cherokee |
| `emoji` | Zero-width joiner sequences, family groups, flag pairs, skin-tone modifiers |
| `rtl_text` | Arabic, Hebrew, Persian, and the RLO override that renders reversed |
| `whitespace` | Leading, trailing, doubled, tab, non-breaking, zero-width, embedded newline |
| `extreme_numbers` | Declared bounds and one inside them; unbounded fields get 2³¹, 2⁵³, 2⁶³ and their negatives |
| `temporal_boundaries` | 29 February, year end, the Unix epoch, and both US DST transitions |
| `extreme_coordinates` | The poles, the antimeridian, null island |

**Never touched:** primary keys, unique fields and references. An emoji primary
key is a broken fixture, not a robustness test — the joins stop resolving and
every finding after that is about Cacophony.

**Applied as each field is produced**, not to the finished record, so anything
derived from an awkward value derives from the awkward value: a first name of
`O'Brien-Smith` yields `o'brien-smith.smith@example.com`, which tests the
template too. Doing it the other way round produced a colleague named
`" leading space"` whose model-written biography still began "Courtney
specializes in…" — two fields disagreeing is a broken fixture, not a finding.

Which records get a case, and which case, is derived from the record index
(section 75), so a bug found once is found again.

---

## Plugins

Design document section 44.

```bash
cacophony plugins
cacophony plugins --show network_packets
```

**Discovery is by installed entry point, never from a project directory, and that
is the decision the feature turns on.** A schema arrives by email, in a Git
repository, inside a `.cacophony` bundle. If Cacophony loaded Python from a
`plugins/` folder beside a schema, opening one somebody sent you would be
equivalent to running their code — and every other safety property here would be
decoration: the expression allow-list, the bundle importer's refusal of
traversal, all of it. The trust decision belongs to a person running
`pip install`, not to a program opening a file.

### Writing one

A plugin is a package with a manifest and a `register` method.

```toml
# pyproject.toml
[project.entry-points."cacophony.plugins"]
network_packets = "my_package:NetworkPackets"
```

```python
from cacophony.core.interfaces import SyncGenerator
from cacophony.generation.generators.base import OptionsMixin


class NetworkPacketGenerator(OptionsMixin, SyncGenerator):
    deterministic = True

    def prepare(self):
        self.protocol = self.opt_choice("protocol", ("tcp", "udp"), "tcp")

    def generate_sync(self, context):
        rng = context.rng()
        return f"{self.protocol}/{rng.randrange(1024, 65536)}"


class NetworkPackets:
    manifest = {
        "name": "network_packets",
        "version": "1.0",
        "provides": {"generators": ["network_packet"]},
    }

    def register(self, host):
        host.add_generator("network_packet", NetworkPacketGenerator, aliases=("packet",))
```

### The eight categories

Section 44's list. Each reaches a registry that already existed — the generator
registry since the first phase, providers since the second, output formats since
the first, transform operations since the twelfth. A plugin is a door into an
extension point, not a mechanism beside one.

| Category | Host method | Reaches |
|---|---|---|
| `generators` | `add_generator(name, cls, aliases=())` | The generator registry (section 8) |
| `transforms` | `add_transform(name, fn)` | Section 105's operations |
| `outputs` | `add_output(name, writer)` | The output formats (section 33) |
| `validators` | `add_validator(name, cls)` | Validation categories (section 57) |
| `scenarios` | `add_scenario(name, cls)` | Scenario behaviours (section 17) |
| `language_models` | `add_language_model(name, cls)` | The provider registry (section 43) |
| `images` | `add_image_provider(name, cls)` | The provider registry (section 18) |
| `speech` | `add_speech_provider(name, cls)` | The provider registry (section 20) |

**The manifest is a contract, checked both ways.** Registering something the
manifest did not declare has it *refused*; declaring something and never
registering it is reported as *missing*. Neither is a security measure — a plugin
is code you installed — but a manifest that has drifted from its code produces a
project that works on one machine and fails on another with no clue why.

A plugin may not take over a name that already exists unless it sets
`replace = True`, because one that silently replaced the built-in `uuid`
generator would change every project on the machine.

**A broken plugin does not stop a run.** One that raises on import is recorded
with its error and skipped, since a plugin installed last month should not stop
today's generation of a project that does not use it. A project that *requires*
it does stop, at compile time, by name.

`CACOPHONY_NO_PLUGINS=1` skips loading entirely — for a run that must be
reproducible against the built-ins alone, or for bisecting a plugin problem.

---

## The desktop application

Design document section 41.

```bash
./desktop/build.sh            # a shell that uses `cacophony` from PATH
./desktop/build.sh --bundle   # an installer, with the backend frozen inside
```

A Tauri window hosting the Studio, with the Python backend as a child process.
Section 41 prefers Tauri to Electron because the application mainly needs to host
a web UI while Python does the generation — which is the architecture that
already existed.

**There is no desktop mode in the backend.** `cacophony desktop` serves the same
application `cacophony serve` serves. Section 41 requires that web deployment
remain possible, and the cheapest way to guarantee that is to have no second
application to keep in step.

```
$ cacophony desktop
CACOPHONY_HANDSHAKE {"version":1,"url":"http://127.0.0.1:41287","token":"…","pid":8123}
```

One line of JSON on stdout, then the server runs. The shell spawns that, reads
the line, and opens a window at the URL.

| Property | Why |
|---|---|
| A port the OS chose | A fixed 8765 collides with the `cacophony serve` already running |
| Printed after binding | A shell that opened a window on a guessed URL would work on fast machines and fail on slow ones |
| A per-launch token | A loopback server is reachable by every process on the machine; a browser tab is an explicit act, a window is not |
| Dies when stdin closes | The one shutdown path that survives the shell being `SIGKILL`ed, which no signal handler does |

The token guards `/api` over **both** HTTP and WebSockets. The Studio itself is
served unauthenticated: static files carrying no data, and the window has to load
before it can present anything. `cacophony serve` passes no token and behaves
exactly as it always has.

### Building a release

The shell is the easy half; the runtime is the hard one. A user who
double-clicks an icon must not need Python, so a bundle freezes an interpreter
and the backend into one executable with PyInstaller, which the shell finds
beside itself as `cacophony-backend`. `CACOPHONY_BACKEND` overrides that, which
is how a checkout points at its own virtualenv.

Neither half cross-compiles — Tauri needs the platform's own webview and
PyInstaller needs the platform's own interpreter — so a release is built once per
platform. `.github/workflows/desktop.yml` does that for Linux, macOS and Windows,
and smoke-tests each frozen backend by reading its handshake.

**Signing is not configured and is deliberately not faked.** macOS notarisation
needs an Apple Developer identity and Windows needs a code-signing certificate;
both are secrets a repository owner supplies. A workflow that pretended to sign
would produce installers that fail on first launch with a message about an
unidentified developer.

---

## The `script` generator

Design document section 8 lists a `script` generator: "a user-provided generator
run in an isolated environment". **It is declared, it compiles, and it is
deliberately not implemented.** That is a decision taken in the plugin phase, not
a postponement, and the reasoning is recorded here because a future reader will
want to reopen it.

*The requirement is absolute.* A project file is shared. If a `script:` field
ran, opening a schema somebody sent you would be equivalent to running their
code.

*A restricted interpreter is not a sandbox.* Stripping `__builtins__` and denying
imports is a denylist, and denylists on a language with introspection are
routinely escaped through object graphs nobody thought about.

*What isolation is available was measured, not assumed.* Unprivileged user and
network namespaces do work on Linux — a subprocess in one cannot open a socket —
but the filesystem stays readable, and blocking it needs a mount namespace and a
pivot into an empty root. That is Linux-only machinery, untestable on the macOS
and Windows the desktop phase targets. A security boundary that exists on one of
three platforms is not a boundary; it is a false sense of one.

*WebAssembly is the honest option and is out of scope.* A CPython build for a
WASM runtime gives real isolation with ceilings enforced by the runtime rather
than by a list. It is also a multi-megabyte dependency and substantial work, and
it is how this should be done when it is done.

**Use instead**, in ascending order of power: `expression` for a derived value,
`patches` for a per-record transformation, and a plugin for arbitrary code — a
package you chose to install, which is where that decision belongs.

A `script` field still compiles, lints, plans and estimates, and
`on_unavailable: placeholder` runs the whole pipeline with a marked stand-in.

---

## Changing a dataset that exists

Sections 104, 105.

```bash
# Rewrite a file. Streams, so the size does not matter.
cacophony transform out/employee.jsonl \
    --set 'email=mask:8' --where "department == 'Finance'" \
    -o out/employee.masked.jsonl --record-as mask_finance_emails

# Apply the project's own patch rules to a file.
cacophony transform out/employee.jsonl --project project.yaml --in-place

# Filter.
cacophony transform out/employee.jsonl --keep-where "department == 'Engineering'" -o eng.jsonl

# Re-derive specific records, with no run and no file.
cacophony regenerate project.yaml -e employee -r 4823913-4823920
```

`jsonl`, `csv` and `json` can be transformed a record at a time. Parquet cannot:
its records live in column chunks, so a row-by-row rewrite would mean decoding
and re-encoding every one. Convert to JSON Lines, transform, convert back.

**It never writes over its input** until the new file is complete — including
`--in-place`, which writes beside and swaps at the end. A rule that raises
halfway through leaves the original untouched and no partial file behind.

**It records what it did** in a `<name>.transform.json` sidecar, because a masked
column is indistinguishable from a column that was always masked.

**`--record-as NAME` prints the equivalent patch rule.** Without it, `transform`
warns that it changed a file and not the project — and that the next `generate`
will produce the untransformed data again. That warning is the honest one: a file
edited outside its schema has stopped being a function of it.

### Regeneration is nearly free

A record's seed is a hash of its position (section 75), so record 4,823,913 can
be produced on its own — without the 4,823,912 before it, without the dataset,
and without the run that made it. "This row looks wrong" is a question with a
millisecond answer. `regenerate` refuses more than a thousand records and points
at `generate`, which is the tool for that.

The Studio's data preview does the same thing from the other end: double-click a
row and it builds a patch rule from it, showing the YAML rather than offering to
save the row.

---

## Bundles

Section 72. A project that can be sent to somebody else.

```bash
cacophony bundle export project.yaml -o team.cacophony
cacophony bundle inspect team.cacophony
cacophony bundle import team.cacophony -d ./team
```

A `.cacophony` file is a zip holding `project.yaml`, a manifest, and the
supporting directories section 72 lists — `recipes/`, `schemas/`, `templates/`,
`workflows/`, `scripts/`, `assets/` — plus `worlds/`, since a named world is
part of what reproduces a dataset.

**Generated data is never included.** Section 72 is explicit, and `.jsonl`,
`.parquet`, `.db` and friends are excluded wherever they sit.

**Files the schema references are packed, wherever they live.** A lookup table in
`data/` is as much part of the project as one in `templates/`, so export follows
the paths in the *expanded* project rather than guessing at directory names.

**A path outside the project directory is refused, not dropped.** It cannot
travel, and a bundle that quietly lost its lookup table would import cleanly and
fail on its first record.

`inspect` writes nothing. It verifies every file against the SHA-256 in the
manifest, then extracts to a temporary directory and *compiles the project* — so
"will this work" is answerable before deciding to unpack it.

**Import treats the archive as untrusted input.** Path traversal, absolute paths,
Windows drive letters and symlink entries are all refused, and the whole archive
is checked before a byte is written, so one bad entry leaves nothing behind
rather than half a project. Size and entry-count ceilings apply: a zip that
expands to a terabyte is not a project.

---

## Model benchmark

Section 67. **Test this model against this schema.**

```bash
cacophony benchmark project.yaml -m gemma4:12b,qwen3:8b -n 100
```

```
MODEL               VALID  FIELDS  USABLE  CLIPPED   SPEED  DUPLICATION  LATENCY
gemma4:12b         100.0%  100.0%   91.7%        1  11 t/s         0.0%  1760 ms
smollm3:latest     100.0%  100.0%   70.0%        6  21 t/s         0.0%   806 ms
```

Records are generated through the real pipeline — the same prompt compiler,
structured-output enforcement, validators and duplicate detection a run uses —
so the numbers mean what they say.

| Column | Meaning |
|---|---|
| `VALID` | Answers that parsed without the repair stage touching them |
| `FIELDS` | Records that passed validation |
| `USABLE` | Values that were not empty, over-length, clipped or a refusal |
| `CLIPPED` | Values that stop dead at their length limit, mid-word |
| `SPEED` | Completion tokens per second |
| `DUPLICATION` | Repeated values, measured with section 59's detector |
| `LATENCY` | Mean per-call latency |

**Fairness is enforced, not documented as a caveat.** Every model generates the
same record indices from the same seed. The cache is forced off — with it on the
second model would be scored on the first model's answers and report an
impossible speed. Concurrency comes from the provider spec and is stated,
because 71 tokens/sec at four concurrent requests is not comparable to 42 at
one.

`CLIPPED` was added after real output showed it. A provider that enforces the
JSON Schema natively stops decoding at `maxLength`, so the value is never *over*
length — it is cut: `"...failed to handle queueing mechanism, led 3"` passes
every check in the platform and is not a sentence. A model needing 140
characters to answer a question given 90 is the wrong model, or the limit is the
wrong limit.

Section 67 also lists *semantic quality*, which cannot be measured without a
judge model — which would make the benchmark depend on the thing it is
assessing. What is measured instead is named honestly: empty values, broken
lengths, clipped answers, and boilerplate about being a language model.

---

## Distributed generation

Sections 84 and 95. A run is cut into shards — contiguous index ranges — and
handed out as leases.

```bash
# One machine, several workers
cacophony cluster templates/security-operations.yaml -o out/ --workers 8

# Several machines
cacophony controller templates/security-operations.yaml --port 8787
cacophony worker templates/security-operations.yaml \
    -c http://controller:8787 -o /mnt/shared
```

Every worker must run the **same project file**, and the same `--seed`,
`--records` and `--format` as the controller. A worker whose schema hashes
differently is refused at registration rather than allowed to contribute
records from a different world.

**Capabilities.** A shard's requirements are read off its compiled generators;
a worker's are read off its configured providers. Neither is declared.

| Capability | A shard needs it when | A worker has it when |
|---|---|---|
| `deterministic` | always | always |
| `language_model` | a field uses the `llm` generator | a `language_model` provider is configured |
| `image` | a field uses the `image` generator | an `image` provider is configured |
| `speech` | a field uses the `tts` generator | a `speech` provider is configured |
| `document` | a field uses the `document` generator | always — rendering is in-process |

`--capabilities deterministic,image` overrides detection, for a node whose
provider is reachable but which you want kept off model work.

**Leases.** A shard is granted for `--lease-seconds` (default 30). The worker
renews halfway through each interval while it generates. If a lease expires the
shard is offered to somebody else, and the original holder is told its lease is
stale and discards what it wrote. After `--max-attempts` failures (default 3) a
shard is marked failed and the run reports it rather than retrying forever.

**Output.** Each shard writes `entity.part<offset>.<ext>` — zero-padded, so an
ordinary directory listing is already in dataset order. `jsonl`, `csv` and
`json` parts join back into one file (`cluster` does it automatically; pass
`--no-join` to keep the parts). `parquet` parts are left as a directory, which
every reader for that format accepts. `sqlite` and `sql` are refused: a
relational output split across shards is not a relational output, because its
foreign keys would not resolve. Generate parts and load them, or use
`cacophony generate`.

**Shared artifacts.** Point every worker's `--assets-dir` at one mounted
directory. Assets are content-addressed (section 81), so two nodes producing
the same file produce the same name with the same bytes. Each node appends to
its own `manifest.<node>.jsonl`; readers read all of them as one run.

**Authentication.** Set `CACOPHONY_CONTROLLER_TOKEN` on the controller to
require it, and the same variable on each worker to send it. It is read from
the environment rather than taken as a flag, so it never lands in a shell
history or a process listing (section 63).

**Controller routes.** `POST /register`, `/lease`, `/renew`, `/complete`,
`/fail`; `GET /status`, `/shards?state=…`, `/health`. `/status` reports
progress, per-worker health and throughput, reassignment counts, and a
`stalled` flag when the remaining work needs a capability no live worker has.

**The output is byte-identical to a single-machine run.** A record's seed is a
hash of its position (section 75), so a shard's records are a pure function of
its index range. That is also why a reassigned shard is regenerated rather than
resumed: the second attempt produces exactly the bytes the first one would
have, so a dead worker's partial file is simply overwritten.

`generate` remains the single-node path, and is the one with run records,
checkpoints and resume. The distributed commands trade that bookkeeping for
parallelism — a shard needs no checkpoint, because if it does not finish it is
simply done again.

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
| `POST` | `/api/projects/{id}/streams` | Start a live stream (sections 35, 94) |
| `GET` | `/api/streams` | Streams this server is running, filterable by project |
| `GET` | `/api/streams/{id}` | Rates, attainment, destinations, per-entity counters |
| `GET` | `/api/streams/{id}/records` | The bounded window of what it just produced |
| `POST` | `/api/streams/{id}/retarget` | Change one entity's rate while it runs |
| `POST` | `/api/streams/{id}/pause` | Hold, keeping the indices |
| `POST` | `/api/streams/{id}/resume` | Carry on |
| `POST` | `/api/streams/{id}/stop` | Stop, waiting for the destinations to close |
| `DELETE` | `/api/streams/{id}` | Stop and forget |
| `WS` | `/api/streams/{id}/feed` | Status, pushed twice a second |
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

## Failure policy

`--on-failure` governs both kinds of failure a record can suffer: a generator
that could not produce a value at all, and a value that was produced and then
found unacceptable. It used to govern only the first, so a run that reported
thirty thousand validation failures wrote those records to the file and exited
successfully. "Abort" meaning "abort unless the data is merely invalid" is not a
promise anybody can plan around, so it now means what it says.

| Policy | A generator that raises | A record that fails validation |
|---|---|---|
| `abort` *(default)* | Stop the run | Stop the run, naming the record and what was wrong |
| `retry` | Try the field again, up to three attempts | Generate the record again; if it still fails, drop it and count it |
| `skip` | Leave the field null | Drop the record and count it |
| `placeholder` | Write `[FAILED:field]` | Mark the offending fields `[FAILED:field]`, keep the record |
| `incomplete` | Write the record without the field | Remove the offending fields, keep the record |
| `report` | Leave the field null | Count it and write it anyway — the old behaviour |

`--drop-invalid` is `skip` for validation whatever the policy says, and
`--no-validate` switches the checking off entirely.

**A run stops at the first invalid record**, so batches already written stay on
disk and the run is recorded as `failed` with the count it reached. It does not
offer to resume, because a record is a pure function of its index and the second
attempt would fail on the same one: the fix is the schema, or a policy that says
carry on.

**`preview`, `regenerate` and `benchmark` report rather than refuse.** All three
exist to *show* you a record that should not exist, so an invalid one is the
answer rather than an error. They still abort on a generator that raises, which
is a different problem and still theirs to surface.

Retrying only helps where a provider is involved. A deterministic field
reproduces the value that failed, so a retried record is the record that just
failed — which is why an exhausted retry drops the record instead of stopping
the run.
