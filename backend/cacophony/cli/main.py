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
        )
        entity = compiled.entity(step.entity)
        for compiled_field in entity.fields:
            marker = "~" if compiled_field.inferred_generator else " "
            style = style_for_generator(compiled_field.generator_name)
            console.print(
                f"   {marker} {compiled_field.name:<26}"
                f"[{style}]{compiled_field.generator.describe()}[/]"
            )
        console.print()

    estimate = generation_plan.estimate
    console.rule(style="cacophony.rule")
    console.print(f"  records            {estimate.records:,}")
    console.print(f"  field values       {estimate.fields:,}")
    console.print(f"  language-model     {estimate.llm_calls:,} calls")
    console.print(f"  images             {estimate.image_calls:,}")
    console.print(f"  audio              {estimate.speech_calls:,}")
    console.print(f"  storage (approx)   {_human_bytes(estimate.estimated_bytes)}")
    console.print(
        "\n[cacophony.muted]Estimates are order-of-magnitude only "
        "(design document section 69). '~' marks an inferred generator.[/]"
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


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #


@app.command()
def generate(
    project: ProjectArg,
    records: Annotated[
        int | None, typer.Option("--records", "-n", help="Override every entity's record count.")
    ] = None,
    seed: SeedOpt = None,
    output: Annotated[
        str, typer.Option("--output", "-o", help=f"One of: {', '.join(sorted(OUTPUT_FORMATS))}.")
    ] = "jsonl",
    out_dir: Annotated[Path, typer.Option("--out-dir", "-d", help="Destination directory.")] = Path(
        "out"
    ),
    entity: EntityOpt = None,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Records per write batch.")
    ] = DEFAULT_BATCH_SIZE,
    workers: Annotated[int, typer.Option("--workers", help="Entities generated concurrently.")] = 4,
    provenance: Annotated[
        str, typer.Option("--provenance", help="none, run, record, field or full.")
    ] = "none",
    on_failure: Annotated[
        str, typer.Option("--on-failure", help=f"One of: {', '.join(FailurePolicy.ALL)}.")
    ] = FailurePolicy.ABORT,
    drop_invalid: Annotated[
        bool, typer.Option("--drop-invalid", help="Discard records that fail validation.")
    ] = False,
    no_validate: Annotated[
        bool, typer.Option("--no-validate", help="Skip validation entirely.")
    ] = False,
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

    config = build_run_config(
        out_dir=out_dir,
        output=output,
        entity=entity,
        records=records,
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
    console.print(f"[cacophony.muted]seed[/] {compiled.seed}   [cacophony.muted]format[/] {output}")
    console.print(f"[cacophony.muted]output[/] {out_dir.resolve()}\n")

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
    reload: Annotated[bool, typer.Option("--reload", help="Reload on source changes.")] = False,
    log_level: LogLevelOpt = "info",
) -> None:
    """Serve the API, the live feed and the Studio (sections 36, 45-56)."""
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

    app = create_app(store_path=store_path, static_dir=studio)
    has_studio = getattr(app.state, "studio_root", None) is not None

    _banner("serve", f"http://{host}:{port}")
    console.print(f"[cacophony.muted]store [/] {store_path}")
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
    from ..providers.base import LanguageModelProvider
    from ..providers.registry import PROVIDER_REGISTRY
    from ..schema.assistant import SchemaAssistant, SchemaProposalError
    from ..schema.models import ProviderSpec

    if out is not None and out.exists() and not force:
        error_console.print(
            f"[cacophony.error]error[/] {out} already exists. Pass --force to overwrite it."
        )
        raise typer.Exit(code=2)

    if provider_from is not None:
        borrowed = _load(provider_from)
        specs = [spec for spec in borrowed.spec.providers.values() if spec.type == "language_model"]
        if not specs:
            error_console.print(
                f"[cacophony.error]error[/] {provider_from} configures no language model"
            )
            raise typer.Exit(code=2)
        spec = specs[0]
        if model:
            spec = spec.model_copy(update={"model": model})
    else:
        spec = ProviderSpec(
            id="assistant",
            type="language_model",
            adapter=adapter,
            base_url=base_url,
            model=model,
        )

    _banner("propose")
    console.print(f"[cacophony.muted]asking[/] {spec.adapter} {spec.model or '(default model)'}")
    console.print(f"[cacophony.muted]about [/] {description.strip()}")
    console.print()

    try:
        provider = PROVIDER_REGISTRY.create(spec)
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc

    if not isinstance(provider, LanguageModelProvider):
        error_console.print(
            f"[cacophony.error]error[/] adapter '{spec.adapter}' is not a language model"
        )
        raise typer.Exit(code=2)

    assistant = SchemaAssistant(provider, model=spec.model)

    async def ask() -> Any:
        try:
            return await assistant.propose(description, seed=seed, scale=scale)
        finally:
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()

    with console.status("[cacophony.muted]designing…[/]", spinner="dots"):
        try:
            proposal = asyncio.run(ask())
        except (SchemaProposalError, CacophonyError) as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=1) from exc

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
            "[cacophony.muted]Pass --out project.yaml to save it, "
            "then 'cacophony generate project.yaml'.[/]"
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


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
