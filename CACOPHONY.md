# CACOPHONY
## Master Design Plan & Master Development Document

**Working Title:** Cacophony  
**Product Type:** Synthetic Data Generation Studio / Bulk Fake Data Platform  
**Primary Deployment:** Local-first desktop/web application  
**Primary AI Backends:** Ollama-compatible APIs and llama.cpp-compatible APIs  
**Optional Generative Backends:** InvokeAI, TTS engines, procedural generators, external adapters  
**Primary Goal:** Generate enormous quantities of realistic, structured, internally consistent, multimodal synthetic data from user-defined schemas.

---

# 1. Executive Summary

Cacophony is a general-purpose synthetic data generation platform designed to create arbitrarily shaped fake datasets at scales ranging from a handful of sample records to millions of records and associated media assets.

Unlike conventional fake-data libraries, Cacophony does not require every field to correspond to a predefined generator such as `first_name`, `phone_number`, or `email`.

Instead, users describe:

- the structure of their records,
- the semantic meaning of each field,
- constraints and validation rules,
- relationships between fields,
- relationships between records,
- desired statistical properties,
- desired degrees of randomness,
- realism requirements,
- and optional multimedia content.

Cacophony interprets these specifications and constructs a generation pipeline.

Individual fields may be produced through:

- deterministic rule-based generators,
- conventional faker libraries,
- random distributions,
- expressions,
- lookup tables,
- local language models,
- image-generation systems such as InvokeAI,
- text-to-speech engines,
- procedural audio generation,
- procedural binary generators,
- scripts or plugins,
- references to previously generated records,
- or combinations of the above.

The system should support datasets such as:

- fictional people,
- corporations,
- employees,
- inventories,
- transactions,
- medical-style test records,
- log events,
- telemetry,
- cybersecurity alerts,
- customer service conversations,
- emails,
- fake social media,
- web traffic,
- legal-style documents,
- fictional news articles,
- photographs,
- avatars,
- product images,
- speech recordings,
- call-center recordings,
- scanned documents,
- OCR datasets,
- chatbot transcripts,
- training corpora,
- game-world datasets,
- and custom domain-specific records.

Cacophony should ultimately be capable of generating not merely random records but entire **synthetic realities** in which records are mutually consistent.

A generated employee can belong to a generated company, use a generated corporate email address, appear in generated security logs, participate in generated email threads, have an associated generated headshot, and speak in generated audio recordings.

That ability to create a coherent artificial world is Cacophony's long-term differentiator.

---

# 2. Product Vision

Cacophony should answer a deceptively simple request:

> “Give me a huge amount of realistic fake data shaped like this.”

The user should not need to write a custom Python generator every time.

The desired workflow is closer to designing a data model.

The user defines:

**WHAT the data looks like**

and

**WHAT the data means.**

Cacophony determines:

**HOW to generate it.**

The system should remain powerful enough that advanced users can override virtually any aspect of generation.

---

# 3. Product Philosophy

## 3.1 Schema First

Everything begins with a project schema.

Schemas describe entities, fields, relationships, constraints, distributions, generation strategies, and outputs.

A schema should be portable and human-readable.

Recommended canonical representation:

```yaml
project:
  name: Example Corporate Dataset

entities:
  employee:
    count: 10000

    fields:
      employee_id:
        type: string
        generator: sequence
        format: "EMP-{000000}"

      first_name:
        type: string
        semantic: "Person's given name"

      last_name:
        type: string
        semantic: "Person's family name"

      title:
        type: string
        semantic: >
          Realistic corporate job title appropriate to this employee's
          department and seniority.

      biography:
        type: text
        generator: llm
        semantic: >
          A short fictional professional biography consistent with the
          employee's department, title, age and location.
```

The GUI should normally generate this schema rather than requiring users to write it.

---

# 4. Core Product Principles

## Local First

Cacophony should be fully usable without transmitting user schemas or generated content to external services.

Local inference should be considered a first-class feature.

## Provider Agnostic

No generation backend should be hard-coded as the only option.

Cacophony should operate through provider interfaces.

## Reproducible

Generation should support explicit seeds.

When deterministic generators are used, identical configuration plus identical seed should produce identical results.

LLM and diffusion generation cannot always be perfectly deterministic across versions, but Cacophony should preserve:

- model name,
- model hash when available,
- generation parameters,
- seed,
- provider version,
- prompt,
- workflow identifier.

## Composable

Fields should be able to depend on other fields.

Entities should be able to depend on other entities.

Generated media should be able to depend on structured records.

## Inspectable

The user should be able to understand why any value was produced.

Each generated value may optionally maintain provenance metadata.

## Scalable

Generating 20 sample records and generating 20 million production-test records should use the same conceptual schema.

## Extensible

Every generator should eventually be replaceable or extendable through plugins.

---

# 5. Major Use Cases

Cacophony should be suitable for:

### Software testing

Generate realistic databases for development and QA environments.

### Information-security testing

Generate:

- SIEM events,
- authentication activity,
- alerts,
- endpoint telemetry,
- phishing simulations,
- vulnerability findings,
- device inventories,
- IAM datasets,
- network activity,
- audit logs.

### Data engineering

Generate large datasets for:

- pipeline load testing,
- transformation testing,
- Spark jobs,
- ETL development,
- database benchmarking,
- schema-evolution testing.

### AI development

Generate:

- classification examples,
- conversations,
- instruction datasets,
- OCR examples,
- image-caption pairs,
- audio-transcription pairs,
- multimodal test corpora.

### UI development

Populate applications with realistic-looking content.

### Demonstrations and sales environments

Construct complete fictional organizations and histories without using customer information.

### Game development

Generate:

- NPCs,
- biographies,
- dialogue,
- inventory,
- quests,
- fictional documents,
- voice lines,
- portraits,
- world histories.

---

# 6. Core Concepts

Cacophony should organize projects around several first-class concepts.

## Project

A complete generation workspace.

Contains:

- schemas,
- entities,
- generators,
- providers,
- output configurations,
- relationships,
- generated runs,
- validation rules,
- seeds,
- project assets.

## Entity

A logical record type.

Examples:

- Person
- Employee
- Computer
- Transaction
- LoginEvent
- Product
- SupportTicket

## Field

A component of an entity.

Fields possess:

- name,
- data type,
- semantic description,
- nullability,
- constraints,
- generation strategy,
- dependencies,
- distribution,
- validation,
- privacy classification.

## Generator

A component capable of producing values.

Examples:

- random integer,
- sequence,
- Faker name,
- weighted category,
- LLM-generated paragraph,
- generated image,
- generated speech.

## Relationship

Connects entities.

Examples:

```text
Company 1 ---- N Employee

Employee 1 ---- N LoginEvent

Employee N ---- 1 Department

Customer 1 ---- N Order
```

## Scenario

A reusable behavioral pattern applied to generated records.

Example:

**Compromised Employee**

May cause:

- suspicious logins,
- impossible travel,
- MFA failures,
- abnormal downloads,
- alert records.

## Run

A single execution of a project.

Stores configuration, progress, statistics, provenance, logs, and output locations.

---

# 7. Supported Primitive Data Types

Initial support should include:

- string
- text
- integer
- float
- decimal
- boolean
- UUID
- date
- time
- datetime
- duration
- enum
- array
- object
- binary
- image
- audio
- file
- URI
- IP address
- CIDR
- MAC address
- hostname
- email
- telephone number
- geographic coordinate
- JSON
- arbitrary user-defined type

Types should provide validation without necessarily determining generation behavior.

---

# 8. Generation Strategies

Every field should expose a **Generation Strategy**.

## Constant

Always generate the same value.

## Sequence

Examples:

```text
1
2
3
4
```

or

```text
USER-000001
USER-000002
```

## Random

Random values within constraints.

## Weighted Choice

Example:

```text
Windows       67%
macOS         18%
Linux         13%
Other          2%
```

## Distribution

Support distributions such as:

- uniform,
- normal,
- log-normal,
- exponential,
- Poisson,
- beta,
- custom histogram.

## Faker

Integrate conventional Faker providers.

Examples:

- names,
- addresses,
- phone numbers,
- company names,
- postal codes,
- credit-card-like test numbers,
- geographic data.

## Pattern

Regex-like or template-based generation.

Example:

```text
SRV-{A-Z}{A-Z}-{0000}
```

## Expression

Fields derived from other values.

Example:

```text
email = lower(first_name + "." + last_name + "@" + company.domain)
```

## Script

User-provided Python or JavaScript generator.

Should run inside an isolated execution environment where practical.

## LLM

Generate semantic content from field and record context.

## Image

Send constructed prompts to an image provider.

## TTS

Generate audio from generated text.

## Dataset Lookup

Select from:

- CSV,
- JSON,
- Parquet,
- SQL result,
- static list,
- another entity.

## Reference

Foreign-key-style reference to another entity.

## Composite

Run multiple generators in sequence.

Example:

1. LLM creates biography.
2. Rule processor strips prohibited content.
3. Validator checks length.
4. LLM retries if invalid.

---

# 9. Semantic Field Annotation

This is a foundational feature.

Each field should have an optional semantic description.

Example:

```text
Field:
resolution_notes

Description:
Natural-language notes written by an IT support technician explaining
how the ticket was resolved.

Style:
Concise internal enterprise IT helpdesk writing.

Constraints:
40–300 characters.

Dependencies:
issue_type
device_type
root_cause
resolution
```

The system uses these annotations to build generation prompts automatically.

Users should rarely need to manually engineer prompts.

---

# 10. LLM Generation Engine

The LLM subsystem should support multiple APIs through adapters.

Proposed interface:

```python
class LanguageModelProvider:
    def list_models(self) -> list[ModelInfo]: ...

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...

    async def health_check(self) -> HealthStatus: ...
```

Initial adapters:

### OllamaProvider

Connect to an Ollama server.

### LlamaCppProvider

Connect to llama.cpp's OpenAI-compatible or native HTTP server.

### OpenAICompatibleProvider

Provide optional compatibility with any server exposing a standard chat/completions interface.

This could indirectly support many inference systems without coupling Cacophony to them.

---

# 11. Intelligent Generation Modes

Cacophony should expose several LLM modes.

## Per Field

Generate one field at a time.

Highest control but expensive.

## Per Record

Ask the LLM for an entire record.

Example:

```json
{
  "department": "Infrastructure",
  "title": "Senior Systems Engineer",
  "bio": "...",
  "specialties": [...]
}
```

## Batch Records

Ask the model to generate perhaps 10–100 records in one call.

Much faster.

## Contextual Expansion

Generate deterministic core fields first, then ask the model to enrich them.

Example:

```text
RULE GENERATED:
Name: Samantha Ortiz
Age: 36
City: Denver
Role: Network Engineer

LLM GENERATED:
Biography
Interests
Support ticket description
Email writing style
```

This will often be the optimal strategy.

---

# 12. Prompt Compiler

Cacophony should contain a dedicated internal **Prompt Compiler**.

It converts schema definitions into provider-specific prompts.

Example conceptual input:

```text
Entity: support_ticket

Fields requiring AI:
subject
description
technician_notes

Known context:
category = VPN
severity = medium
employee.role = Accountant
device.os = Windows 11
```

The compiler might produce:

```text
Generate one fictional enterprise IT support ticket.

The ticket must be internally consistent.

Return STRICT JSON matching this schema...

Known values:
...

Requirements:
...
```

The Prompt Compiler should understand:

- field descriptions,
- types,
- constraints,
- dependencies,
- examples,
- forbidden values,
- tone,
- locale,
- entity context.

---

# 13. Structured Output Enforcement

Never trust raw model output.

LLM results must pass:

1. JSON extraction
2. schema parsing
3. type validation
4. constraint validation
5. dependency validation
6. optional semantic validation

Invalid results should be retried or repaired.

Recommended architecture:

```text
Generate
   ↓
Parse
   ↓
Validate
   ↓
Repair if possible
   ↓
Retry if necessary
   ↓
Accept
```

Use JSON Schema internally where practical.

---

# 14. Cross-Field Coherence

Generated fields must understand each other.

Bad synthetic data:

```text
age = 22
job_title = "Chief Executive Officer"
years_at_company = 31
```

Cacophony should prevent this using field dependencies.

Example:

```text
job_title depends on:
- age
- seniority
- department

years_at_company depends on:
- age
- hire_date
```

A dependency graph should determine generation order.

---

# 15. Cross-Record Coherence

Cacophony should go beyond independent rows.

Examples:

A company contains departments.

Departments contain employees.

Managers should actually exist.

Employees should reference valid managers.

Computers should belong to employees.

Login events should reference those computers.

Email logs should correspond to those employees.

This suggests an internal **Entity Graph**.

```text
Organization
 ├── Departments
 │    └── Employees
 │         ├── Devices
 │         ├── Emails
 │         └── Login Events
 └── Locations
```

Generation executes topologically based on relationships.

---

# 16. Synthetic World Generation

This should become a flagship advanced feature.

Users create a persistent **Synthetic World**.

Example:

### "Acme Test Corporation"

Contains:

- 4,800 employees
- 27 offices
- 8,000 endpoints
- 1,200 servers
- 37 applications
- 14 months of telemetry
- email correspondence
- support tickets
- security incidents
- generated employee photos
- recorded helpdesk calls

Once created, additional datasets can be generated against the same world.

This dramatically improves consistency.

---

# 17. Scenario Engine

A Scenario Engine should modify normal generated behavior.

Example scenario:

## Ransomware Incident

Affected entities:

```text
User:
Robert Chen

Endpoint:
LAPTOP-RCHEN-493

Timeline:
08:02 login
08:17 phishing email opened
08:19 payload execution
08:20 suspicious PowerShell
08:22 credential access
08:26 lateral movement
08:31 file encryption begins
```

Cacophony can then produce related:

- EDR telemetry,
- Windows events,
- SIEM alerts,
- authentication logs,
- emails,
- ticket records,
- analyst notes.

The resulting dataset becomes vastly more useful for security and analytics testing.

---

# 18. Image Generation

Cacophony should provide an image-generation provider interface.

```python
class ImageProvider:
    async def generate(self, prompt, width, height, seed, metadata) -> ImageResult: ...
```

Initial target:

**InvokeAI**

Support concepts such as:

- workflow selection,
- model selection,
- width/height,
- seed,
- steps,
- guidance,
- negative prompt,
- reusable prompt templates.

Possible synthetic image data:

- employee portraits,
- fictional products,
- inventory photographs,
- scanned forms,
- ID-style images,
- building imagery,
- damaged-object examples,
- screenshots,
- synthetic document images.

---

# 19. Image Metadata

The associated record might contain:

```json
{
  "employee_id": "E48291",
  "name": "Samantha Ortiz",
  "portrait": "assets/employees/E48291.png"
}
```

Cacophony should also maintain optional provenance:

```json
{
  "provider": "invokeai",
  "workflow": "employee_portrait_v3",
  "seed": 89324829,
  "prompt_hash": "..."
}
```

---

# 20. Voice and Audio Generation

Create a generic TTS provider:

```python
class SpeechProvider:
    async def synthesize(self, text, voice, options) -> AudioResult: ...
```

Possible integrations may include local TTS systems such as:

- Piper,
- XTTS-class engines,
- other locally hosted TTS providers.

Cacophony should avoid tightly coupling the core architecture to any single TTS engine.

---

# 21. Synthetic Voice Dataset Example

Entity:

```text
customer_service_call
```

Fields:

```text
call_id
customer
agent
issue
conversation
customer_voice
agent_voice
audio_file
transcript_file
duration
sentiment
resolution
```

Pipeline:

```text
Generate customer
      ↓
Generate agent
      ↓
LLM creates conversation
      ↓
Assign synthetic voices
      ↓
TTS renders speakers
      ↓
Audio composer adds pauses
      ↓
Optional background call-center ambience
      ↓
Write WAV/FLAC
      ↓
Create aligned transcript metadata
```

---

# 22. Procedural Audio

Eventually support non-speech audio.

Examples:

- alarms,
- environmental sounds,
- machine noise,
- notification sounds,
- phone-line distortion,
- microphone noise.

These may be produced by plugins or audio synthesis libraries.

---

# 23. Document Generation

Cacophony should generate synthetic documents.

Potential outputs:

- invoices,
- purchase orders,
- resumes,
- support forms,
- shipping labels,
- PDFs,
- receipts,
- reports,
- correspondence.

Pipeline example:

```text
Structured invoice record
      ↓
Template renderer
      ↓
PDF
      ↓
Optional print/scanner degradation
      ↓
Rasterized page image
```

This enables OCR testing datasets.

---

# 24. Data Corruption and Messiness

Real data is ugly.

Cacophony should optionally add controlled imperfection.

Examples:

- null fields,
- malformed records,
- misspellings,
- encoding issues,
- duplicate records,
- whitespace problems,
- incorrect capitalization,
- invalid timestamps,
- inconsistent date formats,
- truncated fields,
- stale references,
- duplicate IDs,
- outlier values.

The user controls corruption percentages.

This feature can be called:

**Entropy Injection**

Example:

```text
Clean records:          95%
Minor defects:           4%
Severely malformed:      1%
```

---

# 25. Temporal Simulation

Many synthetic datasets represent activity over time.

Cacophony should model time intelligently.

Example:

```text
Dataset period:
2025-01-01 → 2025-12-31
```

Events should obey sensible patterns.

Office logins:

- higher Monday–Friday,
- concentrated around work hours,
- reduced on holidays,
- affected by employee timezone.

Web purchases:

- potentially evening-heavy,
- seasonal,
- promotional spikes.

This requires a temporal distribution engine.

---

# 26. Stateful Simulation

Some data cannot be generated correctly as independent events.

Example:

```text
Account balance:
$500
purchase:
-$30
new balance:
$470
```

Therefore Cacophony should support **stateful entity generators**.

A simulation maintains entity state while events are generated.

---

# 27. Generation Pipeline Architecture

Conceptual pipeline:

```text
PROJECT
   ↓
SCHEMA COMPILER
   ↓
DEPENDENCY GRAPH
   ↓
GENERATION PLAN
   ↓
JOB SCHEDULER
   ↓
GENERATOR WORKERS
   ├── Rule Generator
   ├── Faker Generator
   ├── LLM Worker
   ├── Image Worker
   ├── TTS Worker
   ├── Script Worker
   └── Plugin Worker
   ↓
VALIDATION
   ↓
TRANSFORMATION
   ↓
OUTPUT WRITERS
```

---

# 28. Generation Planner

Before executing a run, Cacophony should compile the project into an explicit plan.

Example:

```text
Generate 5 Companies

For each Company:
  Generate Departments

Generate 10,000 Employees

For each Employee:
  Assign Department
  Generate deterministic identity
  Generate LLM biography

Generate portraits for 1,000 selected employees

Generate 3 months of login events
```

The UI should allow users to inspect this plan.

---

# 29. Job System

Long-running generation requires a durable job architecture.

Each run should consist of jobs.

Example job types:

```text
EntityBatchJob
LLMBatchJob
ImageJob
AudioJob
ExportJob
ValidationJob
```

Jobs need states:

```text
Queued
Running
Paused
Retrying
Completed
Failed
Cancelled
```

Store checkpoints so interrupted jobs can resume.

---

# 30. Parallelism

Different providers should have independently configurable concurrency.

Example:

```text
CPU generators:     16 workers
LLM requests:        4 concurrent
InvokeAI:            1 concurrent
TTS:                 2 concurrent
disk writers:        4 workers
```

Backpressure must prevent memory exhaustion.

---

# 31. Streaming Generation

Never require the complete dataset in memory.

Ideal pattern:

```text
Generate batch
     ↓
Validate batch
     ↓
Write batch
     ↓
Release memory
```

This enables datasets far larger than RAM.

---

# 32. Checkpointing

Every generation run should maintain a checkpoint.

Example:

```json
{
  "run": "7ad...",
  "entity": "security_events",
  "completed": 6830000,
  "requested": 10000000,
  "seed_state": "...",
  "last_checkpoint": "..."
}
```

After application restart:

**Resume Run**

---

# 33. Output Formats

Initial support:

- CSV
- JSON
- JSON Lines
- Parquet
- SQLite
- SQL INSERT scripts
- filesystem directories

Later:

- PostgreSQL
- MySQL/MariaDB
- SQL Server
- MongoDB
- Elasticsearch/OpenSearch
- Kafka
- object storage
- HTTP endpoint
- arbitrary plugin output.

---

# 34. Output Profiles

A project may expose multiple output profiles.

Example:

### Analytics

```text
Parquet
partition by year/month/day
```

### Developer DB

```text
SQLite
```

### Application Fixture

```text
JSON
```

Same logical dataset, different outputs.

---

# 35. Live Data Generation

Later Cacophony should support continuous synthetic streams.

Example:

```text
Produce approximately:
250 authentication events/sec
50 endpoint events/sec
8 alerts/minute
```

Possible destinations:

- Kafka
- HTTP
- syslog
- database
- file stream

This transforms Cacophony into a workload generator.

---

# 36. REST API

The backend should expose an API so generation can be automated.

Example conceptual routes:

```text
GET    /api/projects
POST   /api/projects
GET    /api/projects/{id}
POST   /api/projects/{id}/runs

GET    /api/runs/{id}
POST   /api/runs/{id}/pause
POST   /api/runs/{id}/resume
POST   /api/runs/{id}/cancel

GET    /api/providers
GET    /api/providers/{id}/models
POST   /api/providers/{id}/test
```

---

# 37. Command-Line Interface

Cacophony should eventually include a first-class CLI.

Example:

```bash
cacophony generate project.yaml
```

Options:

```bash
cacophony generate project.yaml \
  --records 1000000 \
  --seed 42069 \
  --output parquet
```

Other commands:

```text
cacophony validate
cacophony providers
cacophony preview
cacophony export
cacophony resume
```

---

# 38. Application Architecture

Recommended high-level architecture:

```text
┌────────────────────────────────────────┐
│              Frontend                  │
│ React / TypeScript                     │
└────────────────┬───────────────────────┘
                 │
                 │ REST + WebSocket
                 ▼
┌────────────────────────────────────────┐
│              Backend API               │
│ Python + FastAPI                       │
└────────────────┬───────────────────────┘
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
 Project Service      Generation Engine
       │                   │
       ▼                   ▼
 Metadata DB          Worker Scheduler
                           │
      ┌────────────────────┼────────────────────┐
      ▼                    ▼                    ▼
   LLM Workers       Media Workers       Local Generators
```

---

# 39. Recommended Technology Stack

## Backend

Python 3.12+

Recommended libraries:

- FastAPI
- Pydantic
- SQLAlchemy
- Alembic
- httpx
- Faker
- NumPy
- Polars and/or PyArrow
- Jinja2

Potential job framework:

A lightweight internal asynchronous job manager should be sufficient initially.

Avoid introducing Redis/Celery until distributed execution is actually required.

---

# 40. Frontend

Recommended:

- React
- TypeScript
- Vite
- TanStack Query
- Zustand
- React Flow

React Flow is particularly suitable for:

- entity relationships,
- pipeline visualization,
- scenario graphs,
- dependency editing.

---

# 41. Desktop Packaging

Cacophony should eventually feel like a desktop application while retaining web architecture.

Options:

- Tauri
- Electron

Tauri is preferable if practical because the application primarily needs to host the web UI while the Python backend performs generation.

However, web deployment should remain possible.

---

# 42. Project Database

Use SQLite initially.

Suggested major tables:

```text
projects
entities
fields
relationships
providers
generator_configs
scenarios
runs
jobs
run_statistics
project_assets
validation_rules
```

Generated datasets themselves should generally **not** be stored in this metadata database.

---

# 43. Provider Registry

Providers should register capabilities.

Example:

```json
{
  "id": "local-ollama",
  "type": "language_model",
  "capabilities": [
    "text_generation",
    "structured_output"
  ]
}
```

Another:

```json
{
  "id": "invoke-main",
  "type": "image",
  "capabilities": [
    "text_to_image",
    "image_to_image"
  ]
}
```

---

# 44. Plugin Architecture

Eventually define a plugin protocol.

Plugin categories:

```text
GeneratorPlugin
ValidatorPlugin
TransformPlugin
OutputPlugin
LanguageModelPlugin
ImagePlugin
SpeechPlugin
ScenarioPlugin
```

Plugins should provide manifests describing their capabilities.

Example:

```yaml
name: My Custom Generator
version: 1.0

provides:
  generators:
    - network_packet_generator
```

---

# 45. User Interface Vision

Cacophony should not look like a generic database admin panel.

The visual identity should communicate:

**controlled chaos.**

Possible aesthetic:

- dark graphite background,
- luminous violet,
- electric cyan,
- magenta highlights,
- subtle waveform motifs,
- flowing particle visualizations,
- translucent panels,
- rich data visualizations.

The name **Cacophony** suggests many different generators speaking simultaneously.

A visual motif could involve multicolored signals converging into an organized output stream.

---

# 46. Primary Navigation

Recommended navigation:

```text
Projects
Studio
Generate
Runs
Providers
Assets
Plugins
Settings
```

---

# 47. Project Dashboard

Example:

```text
╔══════════════════════════════════════════════════════╗
║ CACOPHONY                                           ║
║ Synthetic Corporate Security Lab                    ║
╠══════════════════════════════════════════════════════╣
║                                                      ║
║  ENTITIES          OUTPUT                            ║
║  14                8.4M records                     ║
║                                                      ║
║  RELATIONSHIPS     GENERATED MEDIA                  ║
║  22                4,231 files                      ║
║                                                      ║
║  [ OPEN STUDIO ]        [ GENERATE ]                ║
║                                                      ║
╚══════════════════════════════════════════════════════╝
```

---

# 48. Schema Studio

This is the heart of the UI.

Left pane:

```text
Entities

Company
Department
Employee
Device
LoginEvent
SecurityAlert
```

Center:

Entity and field editor.

Right:

Generation properties.

The user can drag fields, inspect dependencies, test generation, and preview records.

---

# 49. Field Editor

Example:

```text
FIELD
────────────────────────────

Name
ticket_description

Type
Long Text

Meaning
Description of the technical problem
written by an employee opening a helpdesk ticket.

Generation
● Language Model

Context
☑ employee
☑ device
☑ ticket_category

Length
100 – 500 characters

Tone
Informal professional

Null probability
0%

[ Generate Samples ]
```

---

# 50. AI-Assisted Schema Creation

This should be an important convenience feature.

User types:

> Generate a schema representing employees, company laptops, login activity, and security alerts for a 5,000-person company.

Cacophony proposes:

```text
Company
Location
Department
Employee
Device
LoginEvent
SecurityAlert
```

plus relationships and fields.

The user approves or edits it.

This turns natural language into structured generator specifications.

---

# 51. Data Preview

Always allow generation of small samples.

Example:

**Generate 25 Preview Records**

Preview table should identify generation sources:

```text
employee_id   name          title               biography
RULE          FAKER         LLM                 LLM
```

Clicking a cell displays provenance.

---

# 52. Distribution Preview

For fields with probabilistic values, show graphs.

Example:

```text
Operating System

Windows   █████████████████████ 68%
macOS     ██████                19%
Linux     ████                  12%
Other     ▏                      1%
```

Allow users to modify distributions interactively.

---

# 53. Relationship Graph

Provide a visual entity relationship canvas.

```text
Company
   │
   ├── Department
   │       │
   │       └── Employee
   │              │
   │              ├── Device
   │              ├── LoginEvent
   │              └── Email
```

This can become an interactive graph.

---

# 54. Generate Screen

Display:

- requested scale,
- estimated workloads,
- provider requirements,
- disk estimate,
- generation plan,
- warnings.

Example:

```text
Records                   12,500,000
Estimated text tokens     8.2M
Images                    5,000
Audio                     1,200
Estimated storage         19.4 GB

LLM       llama3.1:8b
Images    InvokeAI / SDXL
Speech    XTTS

[ START CACOPHONY ]
```

---

# 55. Live Run Visualization

Generation should look satisfying.

Example:

```text
RUNNING
██████████████████░░░░░░░ 72%

Employees       5,000 / 5,000
Devices         7,918 / 8,000
Login Events    3.2M / 5M
Alerts          18,420 / 25,000
Portraits       742 / 1,000
```

Also show throughput:

```text
Records/sec
LLM tokens/sec
Images/minute
Audio minutes/minute
Disk throughput
```

---

# 56. Run Inspector

After generation:

```text
Completed
Duration
Records
Assets
Errors
Retries
Validation failures
Output size
```

Allow browsing rejected records.

---

# 57. Validation System

Validation categories:

## Structural

Correct type and schema.

## Constraint

Example:

```text
age >= 18
```

## Referential

Foreign key exists.

## Logical

Example:

```text
termination_date >= hire_date
```

## Statistical

Generated distributions approximately match target distributions.

## Semantic

Optional LLM evaluation.

Example:

> Does this biography plausibly correspond to the supplied employee profile?

Semantic validation should be optional because of cost.

---

# 58. Quality Metrics

Possible project score:

```text
Schema Validity      100%
Constraint Validity   99.99%
Referential Integrity 100%
Distribution Match    98.7%
LLM Parse Success      99.4%
```

---

# 59. Duplicate Detection

LLMs often repeat themselves.

Cacophony should optionally detect semantic or textual duplication.

Techniques:

- exact hashes,
- normalized hashes,
- n-gram similarity,
- embeddings,
- fuzzy matching.

Users may define allowable duplication thresholds.

---

# 60. Generated Data Provenance

Optional per-field provenance could record:

```json
{
  "generator": "llm",
  "provider": "ollama-local",
  "model": "model-name",
  "seed": 10492,
  "prompt_version": 3
}
```

For huge runs, provenance should be configurable because it can greatly increase storage.

Modes:

```text
None
Run-level
Record-level
Field-level
Full
```

---

# 61. Privacy Guardrails

Cacophony creates synthetic data, but models may accidentally reproduce real information.

Provide optional detectors for:

- real-looking SSNs,
- genuine credit-card numbers,
- known domains,
- suspiciously realistic public identities.

Cacophony should clearly distinguish:

**synthetic representation**

from

**real anonymized data.**

Cacophony should not claim synthetic output is mathematically privacy-preserving unless an appropriate privacy technique is actually used.

---

# 62. Safe Identifier Generation

Built-in generators should avoid accidentally producing valid sensitive identifiers where practical.

Examples:

- reserved domains such as `.example`,
- documented test ranges,
- synthetic account patterns,
- invalid-checksum test numbers when validity is unnecessary.

Allow realistic-valid format only when explicitly requested for application validation testing.

---

# 63. Secret Handling

Provider credentials must never appear inside project files by default.

Use:

- OS keychain,
- environment variables,
- encrypted secret store.

Configurations reference logical secret IDs.

---

# 64. Resource Controls

Generation can consume substantial computing resources.

User settings should include:

```text
Maximum CPU usage
Maximum generation workers
GPU provider concurrency
Memory ceiling
Disk free-space threshold
Request timeout
Maximum retry count
```

---

# 65. Failure Recovery

Individual failures should not destroy massive runs.

Example:

```text
Record 4,823,913 image generation failed.
```

Options:

```text
Retry
Skip
Use placeholder
Mark record incomplete
Abort run
```

Policies should be configurable by generator.

---

# 66. LLM Failure Handling

Potential failures:

- invalid JSON,
- timeout,
- hallucinated fields,
- missing values,
- wrong types,
- model unload,
- server unavailable.

Recommended retry strategy:

```text
Attempt 1:
normal generation

Attempt 2:
repair prompt

Attempt 3:
more explicit schema prompt

Attempt 4:
fallback generator or mark failed
```

Never permit infinite retry loops.

---

# 67. Model Benchmark Tool

An excellent advanced feature:

**Test this model against this schema.**

Cacophony generates perhaps 100 records and measures:

- JSON validity,
- field validity,
- semantic quality,
- throughput,
- repetition,
- average latency.

Users can compare models.

Example:

```text
MODEL               VALID   SPEED   DUPLICATION

Model A             99.8%   42 t/s    0.8%
Model B             96.2%   71 t/s    2.4%
Model C             100%    19 t/s    0.3%
```

---

# 68. Generator Recommendation Engine

Cacophony should not waste LLM calls.

For every field, it can suggest:

```text
first_name
→ Faker recommended

age
→ Statistical generator recommended

employee_number
→ Sequence recommended

biography
→ LLM recommended

portrait
→ Image generator required
```

This is essential for scale.

---

# 69. Cost/Resource Estimation

Before execution calculate approximate workload.

For local inference:

```text
Estimated LLM tokens
Estimated GPU generation time units
Estimated output storage
Estimated memory
```

Avoid pretending estimates are exact.

---

# 70. Template Library

Ship example project templates.

Recommended initial templates:

### Corporate Directory

Employees, departments, locations and devices.

### E-Commerce

Customers, products, orders and payments.

### Helpdesk

Users, devices, tickets and resolutions.

### Security Operations

Endpoints, users, authentication, alerts and incidents.

### SaaS Application

Tenants, accounts, subscriptions and activity.

### IoT Telemetry

Devices, sensors and time-series readings.

### Conversational AI

Users, conversations and labeled intents.

### Multimodal OCR

Documents, images and ground-truth text.

---

# 71. Security Operations Template

Because Cacophony is especially useful here, provide a sophisticated starter project.

Entities:

```text
Organization
User
Device
Application
Authentication
NetworkConnection
EndpointEvent
SecurityFinding
Alert
Incident
Analyst
Ticket
```

Scenarios:

```text
Normal Workday
Password Spray
Impossible Travel
Malware Infection
Phishing
Lateral Movement
Insider Exfiltration
Ransomware
```

---

# 72. Project Portability

Projects should export as bundles.

Example:

```text
my_project.cacophony
```

Internally perhaps:

```text
project.yaml
schemas/
templates/
workflows/
scripts/
assets/
```

Generated datasets should normally be separate.

---

# 73. Schema Versioning

Projects evolve.

Cacophony should track schema revisions.

Example:

```text
v1
Employee.department was string

v2
Employee.department became Department reference
```

Generation runs should record the exact schema revision used.

---

# 74. Git-Friendly Projects

Optional directory-based project mode should be designed for version control.

Avoid unnecessary binary state.

Readable YAML/JSON allows teams to review generator changes in Git.

---

# 75. Deterministic Randomness

Use hierarchical random seeds.

Example:

```text
Project Seed
   ↓
Entity Seed
   ↓
Record Seed
   ↓
Field Seed
```

This allows records to remain reproducible despite parallel execution.

Do not rely solely on one global RNG sequence.

---

# 76. Cache System

Generative content may be expensive.

Cache requests using a content-derived key based on:

- provider,
- model,
- prompt,
- generation settings,
- seed.

Optional cache modes:

```text
Disabled
Read Only
Read/Write
```

---

# 77. Generation Profiles

Different purposes may require different realism.

Profiles:

### Quick Mock

Maximum deterministic generation.

### Balanced

LLM only where useful.

### High Realism

Extensive AI enrichment.

### Maximum Chaos

Heavy entropy injection and edge cases.

---

# 78. "Chaos" Controls

In keeping with the product name, expose an optional **Chaos Panel**.

Possible controls:

```text
Normal variation           75%
Outliers                    5%
Missing data                2%
Duplicates                  1%
Malformed text              1%
Unexpected Unicode          0.5%
Temporal anomalies          0.1%
Referential anomalies       0%
```

Provide presets:

```text
Pristine
Realistic
Messy
Hostile QA
Absolute Cacophony
```

---

# 79. Edge-Case Generation

A special QA mode should deliberately seek weird values.

Examples:

```text
empty strings
maximum lengths
minimum lengths
Unicode names
emoji
apostrophes
hyphenated names
RTL text
huge integers
negative numbers
leap-day dates
DST boundaries
extreme coordinates
```

This makes Cacophony useful for application robustness testing.

---

# 80. Generation Recipes

Allow reusable generator fragments.

Example:

```text
US Corporate Employee Identity
```

Contains:

```text
first name
last name
email
username
employee ID
manager relationship
```

Recipes can be inserted into projects.

---

# 81. Derived Assets

One generated record may create several artifacts.

Example:

```text
Employee
   ├── portrait.png
   ├── id_badge.pdf
   ├── voicemail.wav
   └── signature.png
```

Assets should reference the parent entity.

---

# 82. Artifact Pipeline

Example:

```text
Employee record
     ↓
LLM generates biography
     ↓
Image generator makes portrait
     ↓
Template makes ID badge
     ↓
TTS generates voicemail greeting
```

This is a reusable pipeline graph.

---

# 83. Future Agentic Generation

Eventually a planning agent could interpret requests such as:

> Create a fictional mid-sized hospital organization with approximately 2,000 workers and six months of realistic IT activity.

The agent could:

1. propose entities,
2. generate relationships,
3. assign distributions,
4. select generators,
5. estimate workload,
6. present a plan,
7. generate only after user approval.

The agent should modify schemas—not bypass them.

This ensures every generated world remains inspectable.

---

# 84. Distributed Generation

Not necessary for MVP, but architecture should not prevent it.

Future model:

```text
Cacophony Controller
      │
      ├── CPU Worker Node
      ├── LLM GPU Node
      ├── InvokeAI Node
      ├── TTS Node
      └── Export Node
```

Workers advertise capabilities.

The scheduler routes jobs appropriately.

---

# 85. Remote Providers

Providers should be configured by URI.

Example:

```text
Ollama:
http://gpu-box:11434

llama.cpp:
http://ai-server:8080

InvokeAI:
http://diffusion-box:9090
```

Cacophony itself does not need to own the models.

---

# 86. Observability

Application logs should be structured.

Include:

```text
timestamp
run_id
job_id
provider
entity
record_range
duration
status
error
```

Metrics should include:

```text
records generated
records/sec
provider latency
provider errors
retry rate
validation failures
queue depth
```

---

# 87. Development Logging

Debug mode should optionally preserve:

- prompts,
- raw LLM output,
- parser failures,
- workflow parameters.

These may contain sensitive user-provided information, so debug logs must be clearly controlled.

---

# 88. Testing Strategy

## Unit Tests

Cover:

- generators,
- schema parsing,
- seed derivation,
- validators,
- dependency resolution,
- exporters.

## Integration Tests

Cover:

```text
schema → generator → validation → export
```

Use mock providers.

## Provider Contract Tests

Ensure Ollama, llama.cpp, InvokeAI and TTS adapters obey provider interfaces.

## Property-Based Tests

Very useful for validating generated values across large random samples.

## Scale Tests

Test at:

```text
100
10,000
1,000,000+
```

records.

---

# 89. Performance Goals

Reasonable architectural targets:

For deterministic records:

```text
Tens to hundreds of thousands of fields/sec
```

depending on transformations and storage.

For AI-generated records, throughput will naturally depend on model/provider performance.

The core scheduler must not become the bottleneck.

---

# 90. MVP SCOPE

The first usable Cacophony should focus on structured text data.

## MVP Features

- project creation,
- entity definitions,
- field definitions,
- semantic annotations,
- primitive types,
- constraints,
- Faker generators,
- random generators,
- sequences,
- templates,
- weighted distributions,
- references,
- LLM field generation,
- Ollama provider,
- llama.cpp provider,
- JSON validation,
- preview generation,
- CSV export,
- JSONL export,
- Parquet export,
- reproducible seeds,
- run history,
- background worker execution,
- schema editor GUI.

This is already an extremely useful product.

---

# 91. PHASE 2 — RELATIONAL CACOPHONY

Add:

- relationships,
- dependency graph,
- entity graph UI,
- foreign-key generation,
- expressions,
- stateful record context,
- SQLite output,
- database outputs,
- schema assistant,
- statistical distributions,
- validation dashboards.

---

# 92. PHASE 3 — MULTIMODAL CACOPHONY

Add:

- InvokeAI integration,
- image fields,
- TTS integration,
- audio fields,
- document templates,
- PDF generation,
- asset manager,
- artifact pipelines,
- media metadata.

---

# 93. PHASE 4 — SYNTHETIC WORLDS

Add:

- persistent world state,
- scenario engine,
- temporal simulation,
- stateful simulation,
- correlated event streams,
- entity histories,
- organization generators.

---

# 94. PHASE 5 — LIVE CACOPHONY

Add:

- continuous data generation,
- syslog output,
- HTTP output,
- Kafka output,
- streaming dashboard,
- adjustable event rates,
- long-running simulations.

---

# 95. PHASE 6 — DISTRIBUTED CACOPHONY

Add:

- remote workers,
- capability discovery,
- job leasing,
- distributed generation,
- worker health,
- shared artifact storage.

---

# 96. Suggested Repository Structure

```text
cacophony/
│
├── backend/
│   ├── api/
│   ├── core/
│   ├── schema/
│   ├── generation/
│   │   ├── planner/
│   │   ├── scheduler/
│   │   ├── generators/
│   │   └── workers/
│   ├── providers/
│   │   ├── llm/
│   │   ├── image/
│   │   └── speech/
│   ├── validation/
│   ├── outputs/
│   ├── scenarios/
│   └── plugins/
│
├── frontend/
│   ├── components/
│   ├── pages/
│   ├── studio/
│   ├── graphs/
│   └── state/
│
├── cli/
│
├── templates/
│
├── examples/
│
├── tests/
│
└── docs/
```

---

# 97. Key Backend Interfaces

Core interfaces should be established early.

```python
class Generator:
    async def generate(self, context: GenerationContext) -> GeneratedValue: ...
```

```python
class Provider:
    async def health_check(self): ...

    def capabilities(self): ...
```

```python
class Validator:
    async def validate(self, value, context) -> ValidationResult: ...
```

```python
class OutputWriter:
    async def open(self): ...

    async def write_batch(self, records): ...

    async def close(self): ...
```

This separation is critical.

---

# 98. Generation Context

Every generator should receive a structured context.

Example:

```python
GenerationContext(
    project=...,
    entity=...,
    field=...,
    record_index=...,
    seed=...,
    current_record=...,
    related_records=...,
    scenario=...,
    timeline=...,
)
```

This allows generators to evolve without constantly redesigning interfaces.

---

# 99. Internal Record Representation

Before exporting, records should exist in a provider-neutral representation.

Example:

```python
GeneratedRecord(
    entity="employee",
    id="E10022",
    values={
        "name": "Lisa Hernandez",
        "department": "Security",
        ...
    },
    assets=[],
    provenance={}
)
```

---

# 100. Schema Compiler

The Schema Compiler should:

1. parse project configuration,
2. validate schema,
3. resolve generators,
4. identify dependencies,
5. detect cycles,
6. calculate entity ordering,
7. construct the generation plan.

Circular dependency errors should be surfaced clearly.

---

# 101. Dependency Resolution Example

Suppose:

```text
email depends on first_name + last_name + company

biography depends on age + title + department

portrait_prompt depends on age + appearance_description
```

Correct order:

```text
company
first_name
last_name
age
department
title
appearance_description
email
biography
portrait_prompt
portrait
```

The user should be able to inspect this.

---

# 102. Schema Linter

Before generation, warn about questionable designs.

Examples:

```text
WARNING
Biography uses an LLM but has no semantic description.

WARNING
Employee.manager references Employee and may create unresolved self-reference.

WARNING
Image generation requested for 500,000 records.

WARNING
Field "age" uses uniform distribution from 18–90.
This may be unrealistic for workforce data.
```

---

# 103. Sampling

Users should be able to sample a project repeatedly.

Commands:

```text
Generate One
Generate Ten
Generate 100
```

Sampling must use isolated seeds so previewing does not alter production-run output.

---

# 104. Editing Generated Records

For small datasets, allow manual editing.

For enormous datasets, editing individual rows is inappropriate.

Instead support:

- regeneration,
- transformations,
- filtering,
- patch rules.

---

# 105. Transform Pipeline

Post-generation transformations may include:

```text
lowercase
uppercase
truncate
hash
format date
encode
mask
normalize
add noise
round
compress
```

Custom transforms may be scripted.

---

# 106. Synthetic Data Recipes Worth Supporting Early

Useful built-in generators include:

### Identity

```text
person
employee
customer
username
email
address
```

### Computing

```text
hostname
IP
MAC
OS
browser
device
software version
```

### Security

```text
CVSS-like score
CVE-shaped identifier
alert severity
logon event
network event
hash-like value
```

### Commerce

```text
product
SKU
transaction
invoice
price
currency
```

### Operational

```text
ticket
status
priority
comment
timestamp
```

---

# 107. What Cacophony Should NOT Become

Avoid turning the initial product into:

- a full database management system,
- a generic ETL platform,
- a full ML training framework,
- an image-generation frontend replacement,
- a workflow system competing with Airflow,
- an LLM chat client.

Integrations should support synthetic generation.

They should not overshadow it.

---

# 108. Naming and Terminology

Recommended Cacophony terminology:

```text
Project
Entity
Field
Generator
Provider
Scenario
World
Run
Asset
Recipe
Profile
Chaos
```

Potential branded concepts:

### Cacophony Studio
Schema design environment.

### Conductor
Generation planner/scheduler.

### Voices
Generative providers.

### Score
Project schema.

### Performance
A generation run.

### Chorus
A reusable group of generators.

### Discord
Intentional corruption/edge-case injection.

These musical metaphors could be selectively used without making the product confusing.

A good compromise:

```text
Cacophony Studio
Generation Conductor
Provider Voices
Chaos / Discord controls
```

while retaining standard technical terminology elsewhere.

---

# 109. Example End-to-End Workflow

User creates:

**Enterprise Security Demo**

Defines:

```text
Company
Department
Employee
Device
Login
Alert
```

Employee fields:

```text
name            Faker
age             distribution
department      reference
title           weighted + LLM
email           expression
bio             LLM
portrait        InvokeAI
```

Device:

```text
hostname        pattern
owner           employee reference
OS              weighted
serial          pattern
IP              network allocator
```

Login:

```text
user            employee reference
device          owned device
timestamp       temporal simulation
source_ip       conditional generator
result          weighted
```

The user adds scenario:

**Credential Compromise — 2% of employees**

Cacophony generates:

```text
5,000 employees
7,500 devices
5,000 portraits
15 million login events
22,000 alerts
```

Some compromised users exhibit coordinated abnormal behavior.

Output:

```text
employees.parquet
devices.parquet
logins/
alerts.jsonl
assets/portraits/
```

A complete synthetic enterprise now exists.

---

# 110. Ultimate Vision

Cacophony's ultimate capability should look almost absurdly simple from the user's perspective.

The user says:

> I need fake data for a multinational retail company with 25,000 employees, 200 stores, 18 months of sales transactions, realistic IT activity, customer support tickets, employee headshots, voice recordings from a call center, and a few deliberately planted cybersecurity incidents.

Cacophony constructs a proposed synthetic world.

The user edits the plan.

Then presses:

# BEGIN CACOPHONY

Behind that button:

- deterministic generators construct identities,
- probabilistic models establish distributions,
- LLMs create semantic content,
- relationship engines preserve consistency,
- timeline simulators create history,
- scenario engines inject events,
- InvokeAI generates images,
- TTS generates speech,
- validators reject impossible values,
- exporters stream millions of records to disk,
- metadata preserves reproducibility,
- and the user watches the artificial world come alive.

That should be the long-term destination.

Cacophony is therefore not merely a fake-data generator.

It is a **synthetic reality compiler**.

---

# 111. Recommended First Development Milestone

The first milestone should deliberately prove the core architectural thesis without attempting multimodal generation.

Build:

**Cacophony Studio Alpha**

It must allow the user to:

1. create a project,
2. create entities,
3. create fields,
4. describe fields in natural language,
5. assign deterministic/Faker/LLM generators,
6. configure an Ollama or llama.cpp server,
7. generate preview records,
8. validate those records,
9. generate large datasets using batches,
10. export CSV, JSONL and Parquet,
11. preserve seeds and run history.

The architectural interfaces for image, speech, scenario and plugin providers should already exist, even if their implementations are initially empty.

This ensures that future multimodal functionality extends the platform rather than forcing a rewrite.

---

# 112. Definition of Success

Cacophony Alpha succeeds when a user can describe an unfamiliar data structure without writing custom code and obtain a large, realistic dataset that is:

- structurally valid,
- semantically believable,
- internally consistent,
- reproducible where possible,
- efficiently generated,
- and exportable into practical formats.

Cacophony reaches its full vision when the same mechanism can generate not merely tables, but an entire coherent artificial ecosystem of records, events, documents, images and voices.

**A controlled cacophony of synthetic information.**