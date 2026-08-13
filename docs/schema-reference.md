# Schema reference

The complete Phase 1 project schema. See [CACOPHONY.md](../CACOPHONY.md) for
the design rationale and [ROADMAP.md](ROADMAP.md) for what is not built yet.

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
| `semantic` | What the field **means**, in natural language. Drives generator recommendation and, from Phase 2, prompt compilation. |
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

### Declared, not yet implemented

`llm` `image` `tts` `reference` `script` — see [ROADMAP.md](ROADMAP.md). All
accept `on_unavailable`: `error` (default), `placeholder`, `null`.

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

Relationships affect entity ordering today; foreign-key generation arrives with
Phase 5.

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
```

Credentials never appear in a project file. `secret` names an entry resolved at
run time from the OS keychain, an environment variable or an encrypted store;
the loader rejects anything that looks like a literal key.

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

Formats: `csv`, `json`, `jsonl` / `ndjson`, `parquet`.

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
cacophony generators [--json]
cacophony providers  [project.yaml]
cacophony version
```

Exit codes: `0` success, `1` lint errors, `2` bad schema or bad arguments,
`3` generation failure. Errors go to stderr, so `cacophony preview --json | jq`
is safe.

`--on-failure`: `abort` (default), `retry`, `skip`, `placeholder`, `incomplete`.
