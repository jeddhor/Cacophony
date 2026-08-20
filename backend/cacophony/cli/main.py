"""The Cacophony command-line interface (design document section 37).

    cacophony validate project.yaml
    cacophony lint project.yaml
    cacophony plan project.yaml
    cacophony preview project.yaml --entity employee --count 25
    cacophony generate project.yaml --records 1000000 --seed 42069 --output parquet
    cacophony generators
    cacophony providers project.yaml --test
    cacophony models project.yaml --provider local_llm
    cacophony prompt project.yaml --entity ticket

Every command that reads a project compiles it first, so a schema error is
reported the same way regardless of which command found it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from .. import __version__
from ..core.errors import CacophonyError
from ..core.record import to_jsonable
from ..generation.engine import DEFAULT_BATCH_SIZE, FailurePolicy, GenerationEngine
from ..generation.registry import REGISTRY
from ..generation.runtime import GenerationRuntime
from ..observability.logging import configure_logging
from ..outputs import OUTPUT_FORMATS
from ..providers.cache import CacheMode, GenerationCache
from ..runs.coordinator import Conductor
from ..schema.compiler import compile_project
from ..schema.linter import Severity, lint_project
from ..schema.loader import load_project
from ..schema.plan import CompiledProject
from ..store.database import default_store_path
from .runs import (
    STATE_STYLES,
    build_run_config,
    compile_stored_revision,
    drive,
    open_repository,
    pick_run,
    register_project,
    report_outcome,
)
from .theme import console, error_console, label_for_generator, style_for_generator

app = typer.Typer(
    name="cacophony",
    help="Cacophony - a synthetic reality compiler.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

ProjectArg = Annotated[Path, typer.Argument(help="Path to a project YAML or JSON file.")]
SeedOpt = Annotated[int | None, typer.Option("--seed", help="Override the project seed.")]
EntityOpt = Annotated[
    str | None, typer.Option("--entity", "-e", help="Limit the command to one entity.")
]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _load(path: Path, seed: int | None = None) -> CompiledProject:
    """Load and compile a project, or exit with a readable message."""
    try:
        project = load_project(path)
        if seed is not None:
            project.project.seed = seed
        return compile_project(project)
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc


CacheOpt = Annotated[
    str, typer.Option("--cache", help=f"One of: {', '.join(m.value for m in CacheMode)}.")
]
CachePathOpt = Annotated[
    Path | None, typer.Option("--cache-path", help="Where to keep the generation cache.")
]


StoreOpt = Annotated[Path | None, typer.Option("--store", help="Path to the run store (SQLite).")]
LogLevelOpt = Annotated[str, typer.Option("--log-level", help="debug, info, warning or error.")]
LogFormatOpt = Annotated[str, typer.Option("--log-format", help="text or json.")]


def _runtime(
    compiled: CompiledProject,
    *,
    cache_mode: str = "disabled",
    cache_path: Path | None = None,
    llm_batch_size: int = 20,
) -> GenerationRuntime | None:
    """Build the provider runtime for a project that declares providers."""
    if not compiled.spec.providers:
        return None
    try:
        mode = CacheMode(cache_mode)
    except ValueError as exc:
        error_console.print(
            f"[cacophony.error]error[/] unknown cache mode '{cache_mode}'. "
            f"Choose one of: {', '.join(m.value for m in CacheMode)}"
        )
        raise typer.Exit(code=2) from exc

    cache = GenerationCache(
        cache_path if mode.reads else None,
        mode=mode,
    )
    return GenerationRuntime.for_project(compiled.spec, cache=cache, llm_batch_size=llm_batch_size)


def _banner(title: str, subtitle: str = "") -> None:
    console.print()
    console.print(f"[cacophony.brand]CACOPHONY[/] [cacophony.muted]{title}[/]")
    if subtitle:
        console.print(f"[cacophony.accent]{subtitle}[/]")
    console.rule(style="cacophony.rule")


def _truncate(value: Any, width: int = 38) -> str:
    if value is None:
        return "[cacophony.muted]null[/]"
    text = str(to_jsonable(value))
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


@app.command()
def validate(project: ProjectArg, seed: SeedOpt = None) -> None:
    """Compile the schema and report structural problems."""
    compiled = _load(project, seed)
    plan = compiled.plan
    assert plan is not None

    _banner("validate", compiled.name)
    console.print(f"[cacophony.ok]schema is valid[/]  {project}")
    console.print(f"  entities        {len(compiled.entities)}")
    console.print(f"  records         {plan.estimate.records:,}")
    console.print(f"  field values    {plan.estimate.fields:,}")
    console.print(f"  entity order    {' -> '.join(compiled.entity_order)}")

    inferred = [
        f"{entity.name}.{compiled_field.name}"
        for entity in compiled.ordered_entities()
        for compiled_field in entity.fields
        if compiled_field.inferred_generator
    ]
    if inferred:
        console.print(
            f"\n[cacophony.muted]{len(inferred)} field(s) had a generator inferred. "
            f"Run 'cacophony plan' to see which.[/]"
        )


# --------------------------------------------------------------------------- #
# lint
# --------------------------------------------------------------------------- #


@app.command()
def lint(
    project: ProjectArg,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit non-zero on warnings as well as errors.")
    ] = False,
) -> None:
    """Warn about questionable schema designs (design document section 102)."""
    compiled = _load(project)
    report = lint_project(compiled)

    _banner("lint", compiled.name)
    if not report.issues:
        console.print("[cacophony.ok]no issues found[/]")
        return

    styles = {
        Severity.ERROR: "cacophony.error",
        Severity.WARNING: "cacophony.warn",
        Severity.INFO: "cacophony.info",
    }
    for issue in report.issues:
        console.print(
            f"[{styles[issue.severity]}]{issue.severity.value:<7}[/] "
            f"[cacophony.muted]{issue.code}[/] {issue.location}"
        )
        console.print(f"        {issue.message}")
        if issue.hint:
            console.print(f"        [cacophony.muted]hint: {issue.hint}[/]")

    console.print()
    console.print(
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s), "
        f"{len(report.issues) - len(report.errors) - len(report.warnings)} note(s)"
    )
    if report.errors or (strict and report.warnings):
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


@app.command()
def plan(
    project: ProjectArg,
    seed: SeedOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the plan as JSON.")] = False,
) -> None:
    """Show the compiled generation plan (design document section 28)."""
    compiled = _load(project, seed)
    generation_plan = compiled.plan
    assert generation_plan is not None

    if as_json:
        console.print_json(json.dumps(generation_plan.to_dict()))
        return

    _banner("plan", compiled.name)
    console.print(f"[cacophony.muted]seed[/] {compiled.seed}\n")

    for step in generation_plan.steps:
        console.print(
            f"[cacophony.highlight]Generate {step.count:,} {step.entity}[/]"
            + (
                f"  [cacophony.muted]after {', '.join(step.depends_on)}[/]"
                if step.depends_on
                else ""
            )
            + (f"  [cacophony.muted]tags {', '.join(step.tags)}[/]" if step.tags else "")
        )
        entity = compiled.entity(step.entity)
        for compiled_field in entity.fields:
            marker = "~" if compiled_field.inferred_generator else " "
            style = style_for_generator(compiled_field.generator_name)
            # Which recipe contributed this field (section 80). A schema that
            # silently gains eight fields is a schema nobody can debug, so the
            # plan says where each one came from.
            origin = compiled_field.spec.recipe
            console.print(
                f"   {marker} {compiled_field.name:<26}"
                f"[{style}]{compiled_field.generator.describe()}[/]"
                + (f"  [cacophony.muted]via {origin}[/]" if origin else "")
            )
        console.print()

    estimate = generation_plan.estimate
    console.rule(style="cacophony.rule")
    console.print(f"  records            {estimate.records:,}")
    console.print(f"  field values       {estimate.fields:,}")
    console.print(
        f"  language-model     {estimate.llm_calls:,} calls"
        + (f", ~{estimate.llm_tokens:,} tokens" if estimate.llm_tokens else "")
    )
    console.print(f"  images             {estimate.image_calls:,}")
    console.print(f"  audio              {estimate.speech_calls:,}")
    console.print(f"  storage (approx)   {_human_bytes(estimate.estimated_bytes)}")
    console.print(f"  memory (approx)    {_human_bytes(estimate.peak_memory_bytes)} at a time")
    console.print(
        "\n[cacophony.muted]Estimates are order-of-magnitude only "
        "(design document section 69). '~' marks an inferred generator; "
        "'via' names the recipe a field came from.[/]"
    )


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #


@app.command()
def preview(
    project: ProjectArg,
    entity: EntityOpt = None,
    count: Annotated[int, typer.Option("--count", "-n", help="How many records.")] = 10,
    seed: SeedOpt = None,
    offset: Annotated[int, typer.Option("--offset", help="Start at this record index.")] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit records as JSON Lines.")] = False,
    columns: Annotated[
        str | None,
        typer.Option("--columns", "-c", help="Comma-separated list of fields to show."),
    ] = None,
    isolate: Annotated[
        bool,
        typer.Option(
            "--isolate",
            help="Draw a different sample instead of the records a real run would produce.",
        ),
    ] = False,
    cache: CacheOpt = "disabled",
    cache_path: CachePathOpt = None,
) -> None:
    """Generate a small sample (design document sections 51 and 103).

    By default the preview shows exactly the records a real run would produce
    at those indices. Cacophony derives seeds by hashing a record's position
    rather than by advancing a shared RNG, so sampling cannot disturb a
    production run's output no matter how often you do it. Use --isolate when
    you want a different sample rather than a faithful one.
    """
    compiled = _load(project, seed)
    entity_names = [entity] if entity else list(compiled.entity_order)
    selected_columns = (
        [name.strip() for name in columns.split(",") if name.strip()] if columns else None
    )

    engine = GenerationEngine(
        compiled,
        seed_namespace=f"preview-{time.time_ns()}" if isolate else None,
        failure_policy=FailurePolicy.ABORT,
        # A preview exists to show you what the schema produces, including the
        # records it should not have produced. Refusing to print an invalid
        # record would hide exactly the thing you are previewing for; the run
        # commands are where a validation failure stops the world.
        validation_policy=FailurePolicy.REPORT,
        runtime=_runtime(compiled, cache_mode=cache, cache_path=cache_path),
    )

    if not as_json:
        _banner("preview", compiled.name)

    for name in entity_names:
        try:
            records = engine.preview(name, count, offset=offset)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=3) from exc

        if as_json:
            for record in records:
                print(json.dumps(record.to_dict(jsonable=True), ensure_ascii=False, default=str))
            continue

        _print_preview_table(compiled, name, records, selected=selected_columns)

    if not as_json:
        stats = engine.validation_stats()
        failures = sum(row["records_rejected"] for row in stats.values())
        if failures:
            console.print(f"[cacophony.warn]{failures} record(s) failed validation[/]")
        else:
            console.print("[cacophony.ok]all sampled records passed validation[/]")
        _print_provider_activity(engine)


#: Roughly the narrowest a preview column can be and still say anything.
_MIN_COLUMN_WIDTH = 14


def _print_preview_table(
    compiled: CompiledProject,
    entity_name: str,
    records: list[Any],
    *,
    selected: list[str] | None = None,
) -> None:
    from rich.table import Table

    entity = compiled.entity(entity_name)
    # Columns in authored order, which is what the schema reads like - the
    # engine's dependency order is an implementation detail.
    all_columns = entity.spec.field_names()

    if selected:
        unknown = [name for name in selected if name not in all_columns]
        if unknown:
            error_console.print(
                f"[cacophony.warn]warning[/] {entity_name} has no field(s): {', '.join(unknown)}"
            )
        columns = [name for name in all_columns if name in selected]
        hidden = 0
    else:
        # A wide entity in an 80-column terminal produces an unreadable table.
        # Show what fits and say what was dropped rather than shredding every
        # value into a two-character sliver.
        budget = max(1, (console.width - 4) // _MIN_COLUMN_WIDTH)
        columns = all_columns[:budget]
        hidden = len(all_columns) - len(columns)

    if not columns:
        columns, hidden = all_columns[:1], len(all_columns) - 1

    table = Table(
        title=f"{entity_name}  ({len(records)} records)",
        title_style="cacophony.highlight",
        header_style="cacophony.accent",
        border_style="cacophony.rule",
        expand=False,
    )
    for column in columns:
        table.add_column(column, overflow="fold", max_width=40)

    # Section 51: the preview identifies each column's generation source.
    table.add_row(
        *[
            f"[{style_for_generator(entity.field(column).generator_name)}]"
            f"{label_for_generator(entity.field(column).generator_name)}[/]"
            for column in columns
        ],
        style="cacophony.muted",
    )
    for record in records:
        table.add_row(*[_truncate(record.values.get(column)) for column in columns])

    console.print(table)
    if hidden:
        console.print(
            f"[cacophony.muted]{hidden} more column(s) hidden. "
            f"Use --columns a,b,c to choose, or --json for everything.[/]"
        )


def _profile_dir(compiled: Any, profile: Any) -> Path:
    """Where a profile writes.

    A relative path inside a project resolves against the *schema file*, the
    same rule every other path in a project follows - a lookup table, a document
    template. Without it a project would only work from the directory it lives
    in, which is the thing that rule exists to prevent. ``--out-dir`` is a
    command-line argument and stays relative to where you are standing.
    """
    if profile is None:
        return Path("out")
    declared = Path(profile.path)
    if declared.is_absolute() or compiled.spec.base_dir is None:
        return declared
    return Path(compiled.spec.base_dir) / declared


def _record_counts(compiled: Any, values: list[str] | None) -> tuple[int | None, dict[str, int]]:
    """Parse ``-n 1000`` and ``-n ticket=100000``, which mean different things.

    The bare form overrides *every* entity, which is the blunt instrument people
    reach for when they want a smoke test and are then surprised by a hundred
    thousand employees as well as a hundred thousand tickets. The named form
    exists so that the surprise is avoidable without editing the schema.
    """
    every: int | None = None
    per_entity: dict[str, int] = {}
    for value in values or []:
        name, separator, count = value.partition("=")
        if not separator:
            name, count = "", value
        try:
            number = int(count)
        except ValueError:
            error_console.print(
                f"[cacophony.error]error[/] --records wants a number or ENTITY=NUMBER, "
                f"not '{value}'."
            )
            raise typer.Exit(code=2) from None
        if not name:
            every = number
            continue
        if name not in compiled.entities:
            error_console.print(
                f"[cacophony.error]error[/] no entity '{name}'. "
                f"Known entities: {', '.join(compiled.entity_order)}"
            )
            raise typer.Exit(code=2)
        per_entity[name] = number
    return every, per_entity


def _output_profile(compiled: Any, name: str | None) -> Any:
    """Look up a declared output profile, or exit naming the ones there are.

    Section 34's ``outputs:`` block was parsed and ignored for a long time, so
    an unknown name has to be an error rather than a shrug: silently writing the
    default layout is exactly the failure the block used to have.
    """
    declared = compiled.spec.outputs
    if name is None:
        return None
    profile = declared.get(name)
    if profile is None:
        known = ", ".join(sorted(declared)) or "none are declared"
        error_console.print(
            f"[cacophony.error]error[/] no output profile '{name}'. "
            f"Declared under 'outputs:': {known}"
        )
        raise typer.Exit(code=2)
    return profile


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #


@app.command()
def generate(
    project: ProjectArg,
    records: Annotated[
        list[str] | None,
        typer.Option(
            "--records",
            "-n",
            help="Record count: N for every entity, or ENTITY=N for one. Repeatable.",
        ),
    ] = None,
    seed: SeedOpt = None,
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help=f"One of: {', '.join(sorted(OUTPUT_FORMATS))}."),
    ] = None,
    out_dir: Annotated[
        Path | None, typer.Option("--out-dir", "-d", help="Destination directory.")
    ] = None,
    output_profile: Annotated[
        str | None,
        typer.Option(
            "--output-profile",
            help="Write the layout named under 'outputs:' in the project (section 34).",
        ),
    ] = None,
    entity: EntityOpt = None,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Records per write batch.")
    ] = DEFAULT_BATCH_SIZE,
    workers: Annotated[int, typer.Option("--workers", help="Entities generated concurrently.")] = 4,
    provenance: Annotated[
        str, typer.Option("--provenance", help="none, run, record, field or full.")
    ] = "none",
    on_failure: Annotated[
        str,
        typer.Option(
            "--on-failure",
            help=(
                "When a generator fails or a record fails validation: "
                f"{', '.join(FailurePolicy.ALL)}."
            ),
        ),
    ] = FailurePolicy.ABORT,
    drop_invalid: Annotated[
        bool,
        typer.Option(
            "--drop-invalid",
            help="Discard records that fail validation instead of stopping the run.",
        ),
    ] = False,
    no_validate: Annotated[
        bool, typer.Option("--no-validate", help="Skip validation entirely.")
    ] = False,
    edge_cases: Annotated[
        float,
        typer.Option(
            "--edge-cases",
            help="Fraction of records given a legal-but-awkward value (section 79).",
            min=0.0,
            max=1.0,
        ),
    ] = 0.0,
    edge_categories: Annotated[
        str | None,
        typer.Option(
            "--edge-categories",
            help="Limit edge cases to these, comma-separated. Default: all of them.",
        ),
    ] = None,
    cache: CacheOpt = "disabled",
    cache_path: CachePathOpt = None,
    world: Annotated[
        str | None,
        typer.Option("--world", "-w", help="Generate against a named world (section 16)."),
    ] = None,
    assets_dir: Annotated[
        Path | None,
        typer.Option("--assets-dir", help="Where generated media goes. Default: <out-dir>/assets."),
    ] = None,
    regenerate_assets: Annotated[
        bool,
        typer.Option("--regenerate-assets", help="Redraw media that is already on disk."),
    ] = False,
    llm_batch_size: Annotated[
        int,
        typer.Option("--llm-batch-size", help="Records per language-model call in batch mode."),
    ] = 20,
    checkpoint_every: Annotated[
        int, typer.Option("--checkpoint-every", help="Records between checkpoints.")
    ] = 10_000,
    store: StoreOpt = None,
    no_history: Annotated[
        bool, typer.Option("--no-history", help="Do not record this run in the store.")
    ] = False,
    log_level: LogLevelOpt = "warning",
    log_format: LogFormatOpt = "text",
) -> None:
    """Generate a dataset and stream it to disk (design document section 37).

    The run is recorded, checkpointed and resumable. If it stops - a full disk,
    a killed process, Ctrl-C - ``cacophony resume`` picks it up from the last
    checkpoint rather than from the beginning (section 32).
    """
    configure_logging(log_level, fmt=log_format)
    compiled = _load(project, seed)

    if entity and entity not in compiled.entities:
        error_console.print(
            f"[cacophony.error]error[/] no entity '{entity}'. "
            f"Known entities: {', '.join(compiled.entity_order)}"
        )
        raise typer.Exit(code=2)

    profile = _output_profile(compiled, output_profile)
    every, per_entity = _record_counts(compiled, records)

    config = build_run_config(
        # An explicit flag beats the profile, and the profile beats the default,
        # so `--output-profile analytics -d /tmp/here` writes the analytics
        # layout where you said rather than where the project said.
        out_dir=out_dir or _profile_dir(compiled, profile),
        output=output or (profile.format if profile is not None else "jsonl"),
        profile=profile,
        entity=entity,
        records=every,
        record_counts=per_entity,
        seed=seed,
        no_validate=no_validate,
        drop_invalid=drop_invalid,
        provenance=provenance,
        on_failure=on_failure,
        cache=cache,
        cache_path=cache_path,
        batch_size=batch_size,
        workers=workers,
        llm_batch_size=llm_batch_size,
        checkpoint_every=checkpoint_every,
        record_history=not no_history,
        assets_dir=assets_dir,
        overwrite_assets=regenerate_assets,
        edge_cases=edge_cases,
        edge_categories=edge_categories,
    )

    if world and seed is not None:
        # Two contradictory instructions. Resolving this quietly would break a
        # world's only promise - that it contains the same people - and the
        # user would find out in a join that returns nothing.
        error_console.print(
            f"[cacophony.error]error[/] --world {world} fixes the seed, and --seed {seed} "
            "contradicts it. Drop one: the point of a world is that its people do not change."
        )
        raise typer.Exit(code=2)

    chosen_world = _apply_world(project, compiled, world) if world else None
    if chosen_world is not None:
        # The world's seed has to survive the run config, which is applied by
        # the Conductor after this point.
        config.seed = chosen_world.seed

    repository, project_id, revision_id = register_project(project, compiled, store, config)

    _banner("generate", compiled.name)
    if chosen_world is not None:
        console.print(f"[cacophony.muted]world[/] {chosen_world.name}")
    console.print(
        f"[cacophony.muted]seed[/] {compiled.seed}   "
        f"[cacophony.muted]format[/] {config.output_format}"
        + (f"   [cacophony.muted]profile[/] {config.output_profile}" if profile else "")
        + (
            f"   [cacophony.muted]partitioned by[/] {', '.join(config.partition_by)}"
            if config.partition_by
            else ""
        )
    )
    console.print(f"[cacophony.muted]output[/] {config.output_dir.resolve()}")

    # Deliberate damage and an enforced schema cannot both be had. Say which
    # one gave way, here, rather than leaving it to be discovered later from a
    # constraint that is not in the database.
    if config.output_format.lower() in ("sqlite", "sql") and compiled.spec.chaos.is_enabled():
        console.print(
            "[cacophony.warn]note[/] chaos is enabled, so the tables carry no keys, "
            "uniqueness or NOT NULL - the damage would be rejected by the constraints "
            "it is designed to violate. Indexes are still created."
        )
    console.print()

    conductor = Conductor(
        compiled,
        config,
        repository=repository,
        project_id=project_id,
        revision_id=revision_id,
    )
    outcome = drive(conductor)
    report_outcome(outcome, on_provider_activity=lambda: _print_provider_activity(conductor))


@app.command()
def resume(
    run: Annotated[
        str | None,
        typer.Argument(help="Run id to resume. Omit to take the most recent resumable run."),
    ] = None,
    project: Annotated[
        Path | None, typer.Option("--project", "-p", help="Project whose store to read.")
    ] = None,
    store: StoreOpt = None,
    log_level: LogLevelOpt = "warning",
    log_format: LogFormatOpt = "text",
) -> None:
    """Continue an interrupted run from its checkpoints (section 32).

    The run's original configuration is reused - the same seed, format and
    provenance mode - so a resumed dataset is the one the first attempt was
    producing, not a second dataset generated differently.
    """
    configure_logging(log_level, fmt=log_format)
    repository = open_repository(store, project)
    if repository is None:
        error_console.print(
            "[cacophony.error]error[/] no run store found. Pass --store, or --project to "
            "locate the store beside a schema file."
        )
        raise typer.Exit(code=2)

    stored = pick_run(repository, run)
    compiled = compile_stored_revision(repository, stored)

    _banner("resume", stored["id"][:8])
    completed = sum(job["completed"] for job in stored.get("jobs", []))
    console.print(
        f"[cacophony.muted]state[/] {stored['state']}   "
        f"[cacophony.muted]progress[/] {completed:,} / {stored['records_requested']:,}"
    )
    if stored.get("revision_id") is not None:
        # Section 73: the run continues under the schema it started with, not
        # under whatever the file says now. Saying so avoids a confusing
        # second failure when someone edits the schema and resumes.
        console.print(
            f"[cacophony.muted]schema[/] revision {stored['revision_id']} "
            "(the one this run started with)"
        )
    console.print(f"[cacophony.muted]output[/] {stored.get('output_dir')}\n")

    conductor = Conductor.resume(compiled, stored, repository=repository)
    outcome = drive(conductor, resume=True)
    report_outcome(outcome, on_provider_activity=lambda: _print_provider_activity(conductor))


@app.command(name="runs")
def list_runs(
    project: Annotated[
        Path | None, typer.Option("--project", "-p", help="Project whose store to read.")
    ] = None,
    store: StoreOpt = None,
    state: Annotated[str | None, typer.Option("--state", help="Filter by run state.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many runs to show.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """List recorded runs (design document section 56)."""
    repository = open_repository(store, project)
    if repository is None:
        if as_json:
            print("[]")
            return
        console.print("[cacophony.muted]no run store found[/]")
        return

    rows = repository.list_runs(state=state, limit=limit)
    if as_json:
        console.print_json(json.dumps(rows))
        return

    from rich.table import Table

    _banner("runs", f"{len(rows)} recorded")
    if not rows:
        console.print("[cacophony.muted]no runs yet[/]")
        return

    table = Table(header_style="cacophony.accent", border_style="cacophony.rule")
    table.add_column("run", style="cacophony.highlight")
    table.add_column("state")
    table.add_column("records", justify="right")
    table.add_column("progress", justify="right")
    table.add_column("duration", justify="right")
    table.add_column("started", style="cacophony.muted")

    for row in rows:
        table.add_row(
            row["id"][:8],
            f"[{STATE_STYLES.get(row['state'], 'cacophony.muted')}]{row['state']}[/]",
            f"{row['records_written']:,}",
            f"{row['progress'] * 100:.0f}%",
            f"{row['duration_seconds']:.1f}s" if row["duration_seconds"] else "-",
            (row["started_at"] or row["created_at"] or "")[:19].replace("T", " "),
        )
    console.print(table)
    console.print(
        "\n[cacophony.muted]'cacophony run <id>' inspects one; "
        "'cacophony resume <id>' continues it.[/]"
    )


@app.command(name="run")
def show_run(
    run: Annotated[str, typer.Argument(help="Run id, or a unique prefix of one.")],
    project: Annotated[
        Path | None, typer.Option("--project", "-p", help="Project whose store to read.")
    ] = None,
    store: StoreOpt = None,
    events: Annotated[int, typer.Option("--events", help="How many recent events to show.")] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """The run inspector (design document section 56)."""
    repository = open_repository(store, project)
    if repository is None:
        error_console.print("[cacophony.error]error[/] no run store found")
        raise typer.Exit(code=2)

    stored = pick_run(repository, run)
    if as_json:
        stored["events"] = repository.get_events(stored["id"], limit=events or 200)
        console.print_json(json.dumps(stored))
        return

    _banner("run", stored["id"])
    state = stored["state"]
    console.print(
        f"[{STATE_STYLES.get(state, 'cacophony.muted')}]{state}[/]   "
        f"{stored['records_written']:,} / {stored['records_requested']:,} records"
    )
    if stored["duration_seconds"]:
        rate = stored["records_written"] / stored["duration_seconds"]
        console.print(
            f"  duration        {stored['duration_seconds']:.2f}s  ({rate:,.0f} records/sec)"
        )
    console.print(f"  seed            {stored['seed']}")
    console.print(f"  output          {stored.get('output_dir')} ({stored.get('output_format')})")
    if stored.get("revision_id"):
        console.print(f"  schema revision {stored['revision_id']}")
    if stored.get("error"):
        console.print(f"  [cacophony.error]error[/]           {stored['error']}")

    from rich.table import Table

    console.print()
    table = Table(header_style="cacophony.accent", border_style="cacophony.rule", title="jobs")
    table.add_column("entity", style="cacophony.highlight")
    table.add_column("state")
    table.add_column("done", justify="right")
    table.add_column("requested", justify="right")
    table.add_column("part", justify="right")
    for job in stored.get("jobs", []):
        table.add_row(
            job["entity"] or job["type"],
            f"[{STATE_STYLES.get(job['state'], 'cacophony.muted')}]{job['state']}[/]",
            f"{job['completed']:,}",
            f"{job['requested']:,}",
            str(job["part"]),
        )
    console.print(table)

    # Quality scores are ratios and read as percentages; run counters are
    # counts and read as numbers. Formatting them the same way turned 900
    # records written into "900.00%".
    statistics = stored.get("statistics", [])
    quality = {
        s["name"]: s["value"]
        for s in statistics
        if s["scope"] == "quality" and s["value"] is not None
    }
    counters = {
        s["name"]: s["value"] for s in statistics if s["scope"] == "run" and s["value"] is not None
    }

    if quality:
        console.print("\n[cacophony.accent]quality[/]  (design document section 58)")
        for name, value in sorted(quality.items()):
            console.print(f"  {name:<24} {value:.2%}")
    if counters:
        console.print("\n[cacophony.accent]totals[/]")
        for name, value in sorted(counters.items()):
            rendered = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
            console.print(f"  {name:<24} {rendered}")

    summary = stored.get("summary") or {}
    if summary.get("files"):
        console.print("\n[cacophony.accent]output[/]")
        for path in summary["files"]:
            console.print(f"  {path}")

    if events:
        console.print("\n[cacophony.accent]events[/]")
        for record in repository.get_events(stored["id"], limit=events):
            style = "cacophony.error" if record["level"] == "error" else "cacophony.muted"
            console.print(f"  [{style}]{record['event']:<16}[/] {record['message']}")
    else:
        console.print("\n[cacophony.muted]Pass --events N to see the log.[/]")


def _is_loopback(host: str) -> bool:
    """Whether binding to ``host`` reaches only this machine.

    ``0.0.0.0`` and ``::`` are every interface, which is the case this exists to
    catch. A name that does not parse as an address is treated as reachable:
    guessing that somebody's hostname is private is not a guess worth making.
    """
    import ipaddress

    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    store: StoreOpt = None,
    project: Annotated[
        Path | None, typer.Option("--project", "-p", help="Project whose store to serve.")
    ] = None,
    studio: Annotated[
        Path | None,
        typer.Option("--studio", help="Directory holding a built Studio to serve."),
    ] = None,
    allow_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--allow-root",
            help="A directory requests may name. Repeatable. Required off loopback.",
        ),
    ] = None,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Serve off loopback with no token. For a network you control entirely.",
        ),
    ] = False,
    reload: Annotated[bool, typer.Option("--reload", help="Reload on source changes.")] = False,
    log_level: LogLevelOpt = "info",
) -> None:
    """Serve the API, the live feed and the Studio (sections 36, 45-56).

    On loopback this is as powerful as the shell that started it, which is the
    honest description of a local tool. Bound anywhere else it is a different
    proposition - the API registers projects by path, rewrites their schemas and
    writes runs to a directory the caller names - so a token and a set of
    permitted directories are required. ``CACOPHONY_TOKEN`` carries the token,
    for the reason section 63 gives: a flag lands in shell history and in every
    process listing on the machine.
    """
    try:
        import uvicorn

        from ..api.app import create_app
    except ImportError as exc:
        error_console.print(
            "[cacophony.error]error[/] the API needs FastAPI and uvicorn. "
            "Install them with: pip install 'cacophony[api]'"
        )
        raise typer.Exit(code=2) from exc

    store_path = store or (default_store_path(project) if project else default_store_path())
    configure_logging(log_level)

    token = os.environ.get("CACOPHONY_TOKEN") or None
    roots = [path.expanduser().resolve() for path in (allow_root or [])]
    if not _is_loopback(host):
        if token is None and not insecure:
            error_console.print(
                f"[cacophony.error]error[/] refusing to serve on {host} without a token.\n"
                "This API registers projects by path, rewrites their schemas, and writes\n"
                "runs wherever a request asks. Off loopback that is somebody else's shell.\n\n"
                "  export CACOPHONY_TOKEN=$(python -c "
                "'import secrets;print(secrets.token_urlsafe(32))')\n"
                f"  cacophony serve --host {host} --allow-root .\n\n"
                "Or pass --insecure if this interface is genuinely private."
            )
            raise typer.Exit(code=2)
        if not roots:
            # Somewhere rather than everywhere. The project's own directory is
            # what a served project needs and nothing more.
            roots = [(project.parent if project else Path.cwd()).expanduser().resolve()]

    app = create_app(
        store_path=store_path,
        static_dir=studio,
        token=token,
        allowed_roots=roots or None,
    )
    has_studio = getattr(app.state, "studio_root", None) is not None

    _banner("serve", f"http://{host}:{port}")
    console.print(f"[cacophony.muted]store [/] {store_path}")
    if token is not None:
        console.print("[cacophony.muted]token [/] required on /api (CACOPHONY_TOKEN)")
    elif not _is_loopback(host):
        console.print(
            "[cacophony.warn]note  [/] --insecure: no token, so anyone who can reach "
            f"{host}:{port} can use this API"
        )
    if roots:
        console.print(f"[cacophony.muted]roots [/] {', '.join(str(root) for root in roots)}")
    console.print(f"[cacophony.muted]api   [/] http://{host}:{port}/docs")
    console.print(f"[cacophony.muted]live  [/] ws://{host}:{port}/api/runs/{{run_id}}/stream")
    if has_studio:
        console.print(f"[cacophony.muted]studio[/] http://{host}:{port}/")
    else:
        console.print(
            "[cacophony.muted]studio[/] not built - run 'npm install && npm run build' in frontend/"
        )
    console.print()

    uvicorn.run(app, host=host, port=port, log_level=log_level, reload=reload)


# --------------------------------------------------------------------------- #
# generators / providers / version
# --------------------------------------------------------------------------- #


@app.command()
def generators(
    as_json: Annotated[bool, typer.Option("--json", help="Emit as JSON.")] = False,
) -> None:
    """List every registered generation strategy."""
    rows = REGISTRY.describe()
    if as_json:
        console.print_json(json.dumps(rows))
        return

    from rich.table import Table

    _banner("generators", f"{len(rows)} registered")
    table = Table(header_style="cacophony.accent", border_style="cacophony.rule")
    table.add_column("name", style="cacophony.highlight")
    table.add_column("aliases", style="cacophony.muted")
    table.add_column("needs")
    table.add_column("summary", overflow="fold")

    for row in rows:
        needs = row["requires_provider"] or ""
        table.add_row(
            row["name"],
            ", ".join(row["aliases"]),
            f"[cacophony.warn]{needs}[/]" if needs else "",
            row["summary"],
        )
    console.print(table)


@app.command()
def providers(
    project: Annotated[
        Path | None, typer.Argument(help="Optional project whose providers to list.")
    ] = None,
    test: Annotated[
        bool, typer.Option("--test", help="Probe each configured provider's health.")
    ] = False,
) -> None:
    """List configured providers and available adapters (sections 36, 43)."""
    from ..providers.registry import PROVIDER_REGISTRY
    from ..providers.secrets import DEFAULT_RESOLVER, redact

    _banner("providers")
    adapters = PROVIDER_REGISTRY.adapters()
    console.print(
        f"[cacophony.muted]adapters[/] {', '.join(adapters) if adapters else 'none registered yet'}"
    )

    if project is None:
        return

    compiled = _load(project)
    if not compiled.spec.providers:
        console.print("[cacophony.muted]this project configures no providers[/]")
        return

    console.print()
    for spec in compiled.spec.providers.values():
        console.print(f"[cacophony.highlight]{spec.id}[/]  [cacophony.muted]{spec.type}[/]")
        console.print(f"  adapter      {spec.adapter}")
        console.print(f"  url          {spec.base_url or '-'}")
        console.print(f"  model        {spec.model or '-'}")
        if spec.secret:
            resolved = DEFAULT_RESOLVER.resolve(spec.secret)
            state = redact(resolved) if resolved else "[cacophony.warn]not found[/]"
            console.print(f"  secret id    {spec.secret} -> {state}")
        console.print(f"  concurrency  {spec.concurrency}")

    if not test:
        console.print("\n[cacophony.muted]Pass --test to probe each provider.[/]")
        return

    console.print()
    results = asyncio.run(_probe_providers(compiled))
    unhealthy = 0
    for provider_id, status in results:
        if status.healthy:
            latency = f" ({status.latency_ms:.0f} ms)" if status.latency_ms else ""
            console.print(f"[cacophony.ok]  ok    [/] {provider_id}{latency}  {status.message}")
            for key, value in status.details.items():
                console.print(f"          [cacophony.muted]{key}: {value}[/]")
        else:
            unhealthy += 1
            console.print(f"[cacophony.error]  down  [/] {provider_id}  {status.message}")

    if unhealthy:
        raise typer.Exit(code=1)


async def _probe_providers(compiled: CompiledProject) -> list[tuple[str, Any]]:
    """Health-check every provider a project declares, concurrently."""
    from ..core.errors import CacophonyError as _Error
    from ..core.interfaces import HealthStatus

    runtime = GenerationRuntime.for_project(compiled.spec)
    results: list[tuple[str, Any]] = []

    for provider_id in compiled.spec.providers:
        reason = runtime.is_unavailable(provider_id)
        if reason is not None:
            results.append((provider_id, HealthStatus.down(reason)))
            continue
        try:
            provider = runtime.providers.get(provider_id)
            results.append((provider_id, await provider.health_check()))
        except _Error as exc:
            results.append((provider_id, HealthStatus.down(str(exc))))

    await runtime.aclose()
    return results


@app.command()
def models(
    project: ProjectArg,
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Which provider to ask.")
    ] = None,
) -> None:
    """List the models a provider is serving (design document section 36)."""
    compiled = _load(project)
    if not compiled.spec.providers:
        error_console.print("[cacophony.error]error[/] this project configures no providers")
        raise typer.Exit(code=2)

    wanted = [provider] if provider else list(compiled.spec.providers)
    for provider_id in wanted:
        if provider_id not in compiled.spec.providers:
            error_console.print(
                f"[cacophony.error]error[/] no provider '{provider_id}'. "
                f"Configured: {', '.join(compiled.spec.providers)}"
            )
            raise typer.Exit(code=2)

    _banner("models", compiled.name)
    runtime = GenerationRuntime.for_project(compiled.spec)
    failures = 0

    for provider_id in wanted:
        console.print(f"[cacophony.highlight]{provider_id}[/]")
        try:
            instance = runtime.providers.get(provider_id)
            # Prefer the async lister where an adapter offers one; the
            # synchronous form in the section 10 interface wraps it anyway.
            lister = getattr(instance, "list_models_async", None)
            if lister is not None:
                found = asyncio.run(lister())
            else:
                found = getattr(instance, "list_models", list)()
        except CacophonyError as exc:
            failures += 1
            console.print(f"  [cacophony.error]{exc}[/]")
            continue

        if not found:
            console.print("  [cacophony.muted]no models reported[/]")
        for info in found:
            detail = " ".join(
                part for part in (info.parameter_size, info.quantization, info.family) if part
            )
            console.print(f"  {info.name:<40} [cacophony.muted]{detail}[/]")

    asyncio.run(runtime.aclose())
    if failures:
        raise typer.Exit(code=1)


def _shell_binary() -> str:
    """The Tauri window's executable, if this looks like a checkout.

    A guess, and an honest one: from an installed wheel there is no shell to
    point at, so the build script is named instead of a path that does not
    exist.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[3]
    for candidate in (
        root / "desktop/src-tauri/target/release/cacophony-desktop",
        root / "desktop/src-tauri/target/debug/cacophony-desktop",
    ):
        if candidate.is_file():
            return str(candidate)
    return "./desktop/build.sh   (builds it, as desktop/src-tauri/target/debug/cacophony-desktop)"


@app.command()
def desktop(
    store: StoreOpt = None,
    project: Annotated[
        Path | None, typer.Option("--project", "-p", help="Project whose store to serve.")
    ] = None,
    studio: Annotated[
        Path | None, typer.Option("--studio", help="Directory holding a built Studio.")
    ] = None,
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[
        int | None, typer.Option("--port", help="Fixed port. Default: one the OS says is free.")
    ] = None,
    no_token: Annotated[
        bool, typer.Option("--no-token", help="Do not require a token. For debugging only.")
    ] = False,
    keep_running: Annotated[
        bool,
        typer.Option(
            "--keep-running", help="Do not stop when stdin closes. For running it by hand."
        ),
    ] = False,
    log_level: LogLevelOpt = "warning",
) -> None:
    """Serve the Studio for a desktop shell (design document section 41).

        cacophony desktop

    Prints one handshake line on stdout - the URL, a per-launch token and the
    process id - then serves until stdin closes. The Tauri shell spawns this,
    reads that line and points a window at the URL.

    It is the same application ``cacophony serve`` runs. Section 41 requires that
    web deployment remain possible, and the cheapest way to guarantee that is to
    have no second application to keep in step.
    """
    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:
        error_console.print(
            "[cacophony.error]error[/] the desktop shell needs FastAPI and uvicorn. "
            "Install them with: pip install 'cacophony[api]'"
        )
        raise typer.Exit(code=2) from exc

    from ..desktop import Handshake, run_sidecar

    configure_logging(log_level)
    store_path = store or (default_store_path(project) if project else default_store_path())

    def explain(handshake: Handshake) -> None:
        """Say what this is, to the person who typed it expecting a window.

        Only when a human is watching: the shell spawns this with pipes, so
        stdout is not a terminal and it stays silent. On stderr regardless,
        because the handshake owns stdout.
        """
        if not sys.stdout.isatty():
            return
        address = handshake.url + (f"/?token={handshake.token}" if handshake.token else "")
        error_console.print(
            "\n[cacophony.muted]This is the backend for the desktop window, not the window "
            "itself.[/]\n[cacophony.muted]To open one:[/]"
        )
        # Printed raw and unwrapped: rich highlights a URL by colouring its
        # punctuation, which puts escape sequences inside the address, and wraps
        # long lines - either of which produces something that does not survive
        # being copied out of a terminal.
        error_console.print(f"    {_shell_binary()}", highlight=False, soft_wrap=True)
        error_console.print(
            "[cacophony.muted]or open this in a browser - the token is what gets you in:[/]"
        )
        error_console.print(f"    {address}", highlight=False, soft_wrap=True)
        error_console.print("[cacophony.muted]Ctrl-C to stop.[/]")

    run_sidecar(
        host=host,
        port=port,
        token="" if no_token else None,
        store_path=store_path,
        studio=studio,
        watch_parent=not keep_running,
        log_level=log_level,
        on_ready=explain,
    )


@app.command()
def plugins(
    show: Annotated[str | None, typer.Option("--show", "-s", help="One plugin, in full.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List installed plugins and what they contribute (section 44).

        cacophony plugins
        cacophony plugins --show network_packets

    Plugins are found through the ``cacophony.plugins`` entry point, which means
    a package somebody chose to install. Cacophony deliberately does not load
    Python from a project directory: a schema is something people share, and
    opening one must not be the same as running its author's code.
    """
    from ..plugins import CATEGORIES, ENTRY_POINT_GROUP, load_plugins

    registry = load_plugins(force=True)

    if as_json:
        console.print_json(json.dumps(registry.describe()))
        raise typer.Exit(code=1 if registry.broken else 0)

    if show:
        manifest = registry.by_name(show)
        if manifest is None:
            error_console.print(
                f"[cacophony.error]error[/] no plugin named '{show}'. "
                f"Installed: {', '.join(registry.names()) or '<none>'}"
            )
            raise typer.Exit(code=2)

        _banner("plugin", manifest.name)
        if manifest.description:
            console.print(" ".join(manifest.description.split()))
        console.print(f"[cacophony.muted]version [/] {manifest.version}")
        console.print(f"[cacophony.muted]source  [/] {manifest.source}")
        if manifest.author:
            console.print(f"[cacophony.muted]author  [/] {manifest.author}")
        if manifest.homepage:
            console.print(f"[cacophony.muted]homepage[/] {manifest.homepage}")

        console.print("\n[cacophony.accent]declares[/]")
        for category, names in sorted(manifest.provides.items()):
            console.print(f"  {category:<16} {', '.join(names)}")
        if manifest.registered:
            console.print("[cacophony.accent]registered[/]")
            for category, names in sorted(manifest.registered.items()):
                console.print(f"  {category:<16} {', '.join(names)}")
        for item in manifest.refused:
            error_console.print(
                f"  [cacophony.error]refused[/] {item} - it was not in the manifest"
            )
        for item in manifest.missing:
            error_console.print(
                f"  [cacophony.warn]missing[/] {item} - declared but never registered"
            )
        if manifest.error:
            error_console.print(f"  [cacophony.error]error[/] {manifest.error}")
        raise typer.Exit(code=0 if manifest.ok else 1)

    _banner("plugins", f"{len(registry.manifests)} installed")
    if registry.disabled:
        console.print("[cacophony.warn]loading is disabled by CACOPHONY_NO_PLUGINS[/]\n")

    if not registry.manifests:
        console.print("[cacophony.muted]none installed[/]\n")
        console.print("A plugin is a package that declares itself:")
        console.print("")
        # Escaped: rich would read the brackets as a style tag.
        console.print(f'  \\[project.entry-points."{ENTRY_POINT_GROUP}"]')
        console.print('  network_packets = "my_package:NetworkPackets"')
        console.print(f"\n[cacophony.muted]Categories: {', '.join(sorted(CATEGORIES))}[/]")
        console.print(
            "[cacophony.muted]Cacophony does not load Python from a project directory - "
            "a schema is something people share (section 44).[/]"
        )
        return

    from rich.table import Table

    table = Table(box=None, pad_edge=False, header_style="cacophony.muted")
    table.add_column("plugin")
    table.add_column("version")
    table.add_column("provides")
    table.add_column("state")
    for manifest in sorted(registry.manifests, key=lambda item: item.name):
        provided = ", ".join(
            f"{category} {len(names)}" for category, names in sorted(manifest.provides.items())
        )
        state = "[cacophony.ok]loaded[/]" if manifest.ok else "[cacophony.error]problem[/]"
        table.add_row(manifest.name, manifest.version, provided or "-", state)
    console.print(table)

    for manifest in registry.broken:
        detail = manifest.error or ", ".join([*manifest.refused, *manifest.missing])
        error_console.print(f"\n[cacophony.error]{manifest.name}[/] {detail}")

    contributions = registry.contributions()
    if contributions:
        console.print("\n[cacophony.accent]contributed[/]")
        for category, added in sorted(contributions.items()):
            for name, plugin in sorted(added.items()):
                console.print(f"  {category:<16} {name:<24} [cacophony.muted]{plugin}[/]")

    if registry.broken:
        raise typer.Exit(code=1)


@app.command()
def recipes(
    project: Annotated[
        Path | None,
        typer.Option("--project", "-p", help="Include a project's own recipes and recipes/ dir."),
    ] = None,
    show: Annotated[
        str | None, typer.Option("--show", "-s", help="Print one recipe's fields in full.")
    ] = None,
    group: Annotated[str | None, typer.Option("--group", "-g", help="Only this group.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List the reusable schema fragments (design document sections 80, 106).

        cacophony recipes
        cacophony recipes --show employee

    A recipe is a named group of fields. ``recipes: [employee]`` on an entity
    expands to the twelve fields section 80 lists, and naming one of them again
    overrides it without restating the rest.
    """
    import yaml

    from ..schema.recipes import load_library

    inline: dict[str, Any] = {}
    project_dir: Path | None = None
    if project is not None:
        # Read the document rather than the compiled project: expansion has
        # already consumed `recipes:` by the time a ProjectSpec exists.
        try:
            raw = yaml.safe_load(Path(project).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc
        inline = raw.get("recipes") or {}
        project_dir = Path(project).parent

    try:
        library = load_library(project_dir=project_dir, inline=inline)
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc

    if show:
        try:
            recipe = library.get(show)
            fields = library.resolve(show)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if as_json:
            console.print_json(json.dumps({**recipe.to_dict(), "expanded": fields}))
            return

        _banner("recipe", recipe.name)
        if recipe.description:
            console.print(recipe.description)
        console.print(f"[cacophony.muted]group   [/] {recipe.group}")
        console.print(f"[cacophony.muted]source  [/] {recipe.source}")
        if recipe.includes:
            console.print(f"[cacophony.muted]includes[/] {', '.join(recipe.includes)}")
        if recipe.requires:
            console.print(f"[cacophony.muted]requires[/] {recipe.requires}")
        console.print()
        console.print(f"[cacophony.accent]{len(fields)} field(s)[/]")
        for name, spec in fields.items():
            origin = spec.get("recipe", recipe.name)
            generator = spec.get("generator") or "inferred"
            note = f"  [cacophony.muted]via {origin}[/]" if origin != recipe.name else ""
            console.print(f"  {name:<24} [cacophony.accent]{generator}[/]{note}")
        console.print()
        console.print("[cacophony.muted]use it with:[/]")
        console.print("  entities:")
        console.print("    thing:")
        # Escaped: rich would read the brackets as a style tag.
        console.print(f"      recipes: \\[{recipe.name}]")
        return

    grouped = library.groups()
    if group:
        if group not in grouped:
            error_console.print(
                f"[cacophony.error]error[/] no group '{group}'. Available: {', '.join(grouped)}"
            )
            raise typer.Exit(code=2)
        grouped = {group: grouped[group]}

    if as_json:
        console.print_json(json.dumps(library.describe()))
        return

    _banner("recipes", f"{len(library)} available")
    for name, entries in grouped.items():
        console.print(f"[cacophony.highlight]{name}[/]")
        for recipe in entries:
            summary = " ".join(recipe.description.split())
            # The expanded count, not the recipe's own: `employee` declares five
            # fields and includes three recipes, and somebody choosing between
            # them wants to know they are about to gain thirteen.
            total = len(library.resolve(recipe.name))
            console.print(
                f"  {recipe.name:<18} "
                f"[cacophony.muted]{total} field{'' if total == 1 else 's'}[/]  "
                f"{_truncate(summary, 52)}"
            )
        console.print()
    console.print("[cacophony.muted]cacophony recipes --show NAME  to see a recipe's fields[/]")


@app.command()
def benchmark(
    project: ProjectArg,
    models_arg: Annotated[
        str,
        typer.Option(
            "--models",
            "-m",
            help="Comma-separated model names to compare, e.g. gemma3:12b,qwen3:8b.",
        ),
    ],
    entity: EntityOpt = None,
    records: Annotated[
        int, typer.Option("--records", "-n", help="Records to generate per model.")
    ] = 100,
    provider: Annotated[
        str | None,
        typer.Option("--provider", "-p", help="Which language-model provider to run them on."),
    ] = None,
    sort_by: Annotated[
        str,
        typer.Option("--sort-by", help="json_validity, field_validity, usable, tokens_per_second."),
    ] = "json_validity",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    seed: SeedOpt = None,
) -> None:
    """Test models against this schema (design document section 67).

        cacophony benchmark project.yaml -m gemma3:12b,qwen3:8b -n 100

    Every model generates the same records from the same seed, with the cache
    forced off, so the comparison is of the models rather than of what happened
    to be cached. A model that reasons beautifully and cannot reliably return a
    JSON object with two string fields is the wrong model for this job, and this
    is what says so.
    """
    from ..generation.benchmark import default_entity, render_table, run_benchmark

    compiled = _load(project, seed)
    wanted = [name.strip() for name in models_arg.split(",") if name.strip()]
    if not wanted:
        error_console.print("[cacophony.error]error[/] --models needs at least one model name")
        raise typer.Exit(code=2)

    try:
        target = entity or default_entity(compiled)
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc

    if not as_json:
        _banner("benchmark", f"{compiled.name} · {target}")
        console.print(
            f"[cacophony.muted]models[/] {', '.join(wanted)}   "
            f"[cacophony.muted]records[/] {records:,} each   "
            f"[cacophony.muted]seed[/] {compiled.seed}"
        )
        console.print(
            "[cacophony.muted]cache[/] disabled, so nobody is scored on somebody else's answers\n"
        )

    try:
        result = run_benchmark(
            compiled,
            wanted,
            entity=target,
            records=records,
            provider=provider,
            on_model=(
                None
                if as_json
                else lambda name: console.print(f"[cacophony.muted]running[/] {name}…")
            ),
        )
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc

    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(code=0 if result.ok else 1)

    from rich.table import Table

    rows = render_table(result, by=sort_by)
    table = Table(box=None, pad_edge=False, header_style="cacophony.muted")
    for index, heading in enumerate(rows[0]):
        table.add_column(heading, justify="left" if index == 0 else "right")
    for row in rows[1:]:
        table.add_row(*row)

    console.print()
    console.print(table)
    console.print(
        "\n[cacophony.muted]VALID[/] answers that parsed without repair   "
        "[cacophony.muted]FIELDS[/] records that passed validation   "
        "[cacophony.muted]USABLE[/] values fit to keep\n"
        "[cacophony.muted]CLIPPED[/] values cut off mid-word at their length limit   "
        "[cacophony.muted]DUPLICATION[/] repeated values"
    )
    if sum(score.clipped for score in result.scores if score.ok):
        console.print(
            "[cacophony.warn]note[/] some values stop dead at their length limit. A provider "
            "that enforces the schema natively cuts the answer rather than exceeding it, so "
            "the limit is doing the writing - raise max_length, or ask for less."
        )

    for score in result.scores:
        if score.error:
            error_console.print(f"[cacophony.error]{score.model}[/] {score.error}")
        elif score.concurrency > 1:
            console.print(
                f"[cacophony.muted]{score.model} ran at concurrency {score.concurrency}; "
                "a throughput compared across different concurrencies is not a comparison[/]"
            )

    if not result.ok:
        raise typer.Exit(code=1)


@app.command()
def prompt(
    project: ProjectArg,
    entity: EntityOpt = None,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Batch size to render for batch-mode fields.")
    ] = 3,
    show_schema: Annotated[
        bool, typer.Option("--schema", help="Also print the JSON Schema.")
    ] = False,
) -> None:
    """Show the prompts the compiler builds (design document section 12).

    Section 9 says users should rarely need to engineer prompts by hand. This
    is how they check what was written on their behalf.
    """
    from ..generation.enrichment import plan_enrichment

    compiled = _load(project)
    runtime = GenerationRuntime.for_project(compiled.spec, create_providers=False)
    entity_names = [entity] if entity else list(compiled.entity_order)

    _banner("prompt", compiled.name)
    found = False

    for name in entity_names:
        compiled_entity = compiled.entity(name)
        for layer in compiled_entity.layers():
            ai_fields = [
                field
                for field in layer
                if type(field.generator).requires_provider == "language_model"
            ]
            if not ai_fields:
                continue
            found = True
            for group in plan_enrichment(
                compiled_entity, ai_fields, runtime, batch_size=batch_size
            ):
                console.rule(
                    f"[cacophony.highlight]{name}: {group.describe()}", style="cacophony.rule"
                )
                console.print(f"[cacophony.muted]prompt hash {group.prompt.hash}[/]\n")
                console.print("[cacophony.accent]SYSTEM[/]")
                console.print(group.prompt.system)
                console.print("\n[cacophony.accent]USER[/]")
                console.print(group.prompt.instruction)
                if group.context_fields:
                    console.print(
                        f"\n[cacophony.muted]plus known values for: "
                        f"{', '.join(group.context_fields)}[/]"
                    )
                if show_schema:
                    console.print("\n[cacophony.accent]JSON SCHEMA[/]")
                    console.print_json(json.dumps(group.prompt.json_schema))
                console.print()

    if not found:
        console.print("[cacophony.muted]no language-model fields in this project[/]")


def _print_provider_activity(owner: GenerationEngine | Conductor) -> None:
    """Report what the providers actually did (sections 58, 86).

    Takes an engine or a conductor: both hold a runtime, and both want to say
    the same thing about it.
    """
    runtime = getattr(owner, "runtime", None)
    if runtime is None:
        return

    for provider_id, reason in runtime.unavailable.items():
        console.print(f"[cacophony.warn]provider '{provider_id}' unavailable[/] {reason}")

    stats = runtime.stats
    cache = runtime.cache
    # A run served entirely from cache makes no calls at all, and that is
    # precisely the fact worth reporting - so the cache counters, not the call
    # count, decide whether there is anything to say.
    if not stats.llm_calls and not cache.stats.hits:
        return

    console.print()
    console.print(f"  language-model calls  {stats.llm_calls:,}")
    console.print(f"  records enriched      {stats.records_enriched:,}")
    console.print(
        f"  tokens                {stats.prompt_tokens:,} in / {stats.completion_tokens:,} out"
    )
    console.print(f"  mean latency          {stats.mean_latency_ms:.0f} ms")
    if stats.llm_retries:
        console.print(f"  [cacophony.warn]retries               {stats.llm_retries:,}[/]")
    if stats.parse_failures:
        console.print(f"  [cacophony.warn]parse failures        {stats.parse_failures:,}[/]")
    if stats.repairs:
        console.print(f"  repairs               {stats.repairs:,}")
    if stats.fallbacks:
        console.print(f"  [cacophony.warn]degraded fields       {stats.fallbacks:,}[/]")
    if runtime.cache.mode.reads:
        cache = runtime.cache.describe()
        console.print(
            f"  cache                 {cache['hits']:,} hits / {cache['misses']:,} misses"
        )


# --------------------------------------------------------------------------- #
# propose
# --------------------------------------------------------------------------- #


@app.command()
def propose(
    description: Annotated[
        str, typer.Argument(help="What the data should represent, in plain language.")
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write the proposal here instead of to the screen."),
    ] = None,
    provider_from: Annotated[
        Path | None,
        typer.Option(
            "--providers",
            help="A project file whose provider configuration to borrow.",
        ),
    ] = None,
    adapter: Annotated[
        str, typer.Option("--adapter", help="Provider adapter to use when none is borrowed.")
    ] = "ollama",
    base_url: Annotated[str | None, typer.Option("--url", help="Provider base URL.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model to ask.")] = None,
    scale: Annotated[
        int | None,
        typer.Option("--scale", help="Divide every proposed record count by this."),
    ] = None,
    seed: SeedOpt = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite the output file if it exists.")
    ] = False,
) -> None:
    """Propose a schema from a description (design document section 50).

    The model proposes the entities, fields and relationships; Cacophony picks
    the generators, compiles the result and lints it before showing it. What
    you get back is a schema that is known to work - and a file to edit.
    """
    from .proposing import ask_for_a_schema, provider_spec_for

    if out is not None and out.exists() and not force:
        error_console.print(
            f"[cacophony.error]error[/] {out} already exists. Pass --force to overwrite it."
        )
        raise typer.Exit(code=2)

    spec = provider_spec_for(
        provider_from=provider_from, adapter=adapter, base_url=base_url, model=model
    )

    _banner("propose")
    console.print(f"[cacophony.muted]asking[/] {spec.adapter} {spec.model or '(default model)'}")
    console.print(f"[cacophony.muted]about [/] {description.strip()}")
    console.print()

    proposal = ask_for_a_schema(spec, description, seed=seed, scale=scale)

    summary = proposal.summary()
    console.print(
        f"[cacophony.ok]proposed[/] {len(summary['entities'])} entities, "
        f"{summary['records']:,} records"
    )
    console.print(f"  entity order  {' -> '.join(summary['entities'])}")
    if proposal.attempts > 1:
        console.print(f"  [cacophony.muted]attempts      {proposal.attempts}[/]")
    for note in proposal.notes:
        console.print(f"  [cacophony.warn]note[/]          {note}")

    if proposal.lint is not None and len(proposal.lint):
        console.print()
        console.print(proposal.lint.render())

    if out is None:
        console.print()
        console.print(proposal.yaml)
        console.print(
            "[cacophony.muted]Pass --out project.yaml to save it, then "
            "'cacophony generate project.yaml' - or 'cacophony begin' to do all of it.[/]"
        )
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(proposal.yaml, encoding="utf-8")
    console.print()
    console.print(f"[cacophony.ok]written[/] {out}")
    console.print(f"[cacophony.muted]next[/] cacophony preview {out}")


# --------------------------------------------------------------------------- #
# worlds
# --------------------------------------------------------------------------- #


def _apply_world(project: Path, compiled: CompiledProject, name: str) -> Any:
    """Generate against a named world, warning if the schema has moved on.

    A conflict is reported rather than refused: the user may genuinely have
    changed the schema. What must never happen silently is a run producing
    *different* people under a world's name (section 16).
    """
    from ..simulation.world import WorldStore

    store = WorldStore(Path(project).parent / ".cacophony")
    world = store.get(name)
    if world is None:
        known = ", ".join(store.names()) or "<none>"
        error_console.print(
            f"[cacophony.error]error[/] no world named '{name}'. Known: {known}. "
            f"Create one with: cacophony worlds {project} --create {name}"
        )
        raise typer.Exit(code=2)

    for problem in world.conflicts_with(compiled):
        error_console.print(f"[cacophony.warn]warning[/] {problem}")
    world.apply_to(compiled)
    return world


@app.command(name="worlds")
def list_worlds(
    project: ProjectArg,
    create: Annotated[
        str | None, typer.Option("--create", "-c", help="Record this project as a named world.")
    ] = None,
    delete: Annotated[str | None, typer.Option("--delete", help="Forget a world.")] = None,
    describe: Annotated[
        str | None, typer.Option("--show", help="Show one world and check it still matches.")
    ] = None,
) -> None:
    """Named, reproducible populations (design document section 16).

    A world is a name for a seed and the schema that goes with it. Generating
    against one produces the same people every time, so a dataset of logins made
    today and a dataset of tickets made next week describe the same company.
    """
    from ..simulation.world import World, WorldStore

    compiled = _load(project)
    store = WorldStore(Path(project).parent / ".cacophony")
    _banner("worlds", compiled.name)

    if delete:
        console.print(
            f"[cacophony.ok]forgotten[/] {delete}"
            if store.delete(delete)
            else f"[cacophony.muted]no world named {delete}[/]"
        )
        return

    if create:
        world = store.save(World.of(create, compiled))
        console.print(f"[cacophony.ok]created[/] {world.name}")
        console.print(f"  seed          {world.seed}")
        console.print(f"  schema        {world.schema_hash[:16]}")
        for name, size in world.populations.items():
            console.print(f"  {name:<13} {size:,}")
        console.print(
            f"\n[cacophony.muted]generate against it:[/] "
            f"cacophony generate {project} --world {create}"
        )
        return

    worlds = list(store)
    if describe:
        found = store.get(describe)
        if found is None:
            error_console.print(f"[cacophony.error]error[/] no world named '{describe}'")
            raise typer.Exit(code=2)
        console.print(f"[cacophony.highlight]{found.name}[/]  seed {found.seed}")
        if found.description:
            console.print(f"  {found.description}")
        console.print(f"  created       {found.created_at}")
        for name, size in found.populations.items():
            console.print(f"  {name:<13} {size:,}")
        if found.runs:
            console.print(f"  drawn from by {len(found.runs)} run(s)")

        problems = found.conflicts_with(compiled)
        console.print()
        if problems:
            for problem in problems:
                console.print(f"  [cacophony.warn]changed[/] {problem}")
        else:
            console.print("  [cacophony.ok]this project still produces this world's people[/]")
        return

    if not worlds:
        console.print("[cacophony.muted]no worlds recorded[/]")
        console.print(f"[cacophony.muted]create one:[/] cacophony worlds {project} --create acme")
        return

    for world in worlds:
        total = sum(world.populations.values())
        console.print(
            f"[cacophony.highlight]{world.name:<18}[/] seed {world.seed:<12} "
            f"{total:,} records  {len(world.runs)} run(s)"
        )


@app.command()
def version() -> None:
    """Show the Cacophony version."""
    console.print(f"[cacophony.brand]Cacophony[/] {__version__}")


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


from .afterwards import register as _register_afterwards  # noqa: E402
from .begin import register as _register_begin  # noqa: E402
from .bundles import register as _register_bundles  # noqa: E402
from .distributed import register as _register_distributed  # noqa: E402
from .stream import register as _register_stream  # noqa: E402

_register_stream(app)
_register_distributed(app)
_register_bundles(app)
_register_afterwards(app)
_register_begin(app)


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
