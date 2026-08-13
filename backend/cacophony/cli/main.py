"""The Cacophony command-line interface (design document section 37).

    cacophony validate project.yaml
    cacophony lint project.yaml
    cacophony plan project.yaml
    cacophony preview project.yaml --entity employee --count 25
    cacophony generate project.yaml --records 1000000 --seed 42069 --output parquet
    cacophony generators
    cacophony providers project.yaml

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
from ..core.provenance import ProvenanceMode
from ..core.record import to_jsonable
from ..generation.engine import DEFAULT_BATCH_SIZE, FailurePolicy, GenerationEngine
from ..generation.registry import REGISTRY
from ..outputs import OUTPUT_FORMATS, create_writer, output_path_for
from ..schema.compiler import compile_project
from ..schema.linter import Severity, lint_project
from ..schema.loader import load_project
from ..schema.plan import CompiledProject
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
) -> None:
    """Generate a dataset and stream it to disk (design document section 37)."""
    compiled = _load(project, seed)

    try:
        provenance_mode = ProvenanceMode(provenance)
    except ValueError as exc:
        error_console.print(
            f"[cacophony.error]error[/] unknown provenance mode '{provenance}'. "
            f"Choose one of: {', '.join(mode.value for mode in ProvenanceMode)}"
        )
        raise typer.Exit(code=2) from exc

    if output.lower() not in OUTPUT_FORMATS:
        error_console.print(
            f"[cacophony.error]error[/] unknown output format '{output}'. "
            f"Available: {', '.join(sorted(OUTPUT_FORMATS))}"
        )
        raise typer.Exit(code=2)

    entity_names = [entity] if entity else list(compiled.entity_order)
    for name in entity_names:
        if name not in compiled.entities:
            error_console.print(
                f"[cacophony.error]error[/] no entity '{name}'. "
                f"Known entities: {', '.join(compiled.entity_order)}"
            )
            raise typer.Exit(code=2)

    _banner("generate", compiled.name)
    console.print(f"[cacophony.muted]seed[/] {compiled.seed}   [cacophony.muted]format[/] {output}")
    console.print(f"[cacophony.muted]output[/] {out_dir.resolve()}\n")

    engine = GenerationEngine(
        compiled,
        validate=not no_validate,
        drop_invalid=drop_invalid,
        provenance=provenance_mode,
        failure_policy=on_failure,
    )

    try:
        totals = asyncio.run(
            _run_generation(
                compiled=compiled,
                engine=engine,
                entity_names=entity_names,
                records=records,
                fmt=output,
                out_dir=out_dir,
                batch_size=batch_size,
                provenance_mode=provenance_mode,
            )
        )
    except CacophonyError as exc:
        error_console.print(f"\n[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=3) from exc

    console.rule(style="cacophony.rule")
    console.print(
        f"[cacophony.ok]complete[/]  {totals['records']:,} records in {totals['seconds']:.2f}s"
    )
    if totals["seconds"] > 0:
        console.print(f"  throughput      {totals['records'] / totals['seconds']:,.0f} records/sec")
    console.print(f"  files           {totals['files']}")

    rejected = sum(stats.rejected for stats in engine.stats.values())
    if rejected:
        console.print(f"  [cacophony.warn]validation failures  {rejected:,}[/]")


async def _run_generation(
    *,
    compiled: CompiledProject,
    engine: GenerationEngine,
    entity_names: list[str],
    records: int | None,
    fmt: str,
    out_dir: Path,
    batch_size: int,
    provenance_mode: ProvenanceMode,
) -> dict[str, Any]:
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    started = time.perf_counter()
    total_records = 0
    files: list[str] = []

    with Progress(
        SpinnerColumn(style="cacophony.highlight"),
        TextColumn("[cacophony.accent]{task.description}"),
        BarColumn(complete_style="cacophony.brand", finished_style="cacophony.ok"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        for name in entity_names:
            entity = compiled.entity(name)
            count = records if records is not None else entity.count
            if count <= 0:
                continue

            path = output_path_for(out_dir, name, fmt)
            writer = create_writer(
                fmt,
                path,
                columns=entity.spec.field_names(),
                provenance=provenance_mode,
            )
            task = progress.add_task(name, total=count)

            await writer.open()
            try:
                async for batch in engine.stream(name, count=count, batch_size=batch_size):
                    await writer.write_batch(batch)
                    progress.advance(task, len(batch))
                    total_records += len(batch)
            finally:
                await writer.close()

            files.append(str(path))

    return {
        "records": total_records,
        "seconds": time.perf_counter() - started,
        "files": len(files),
        "paths": files,
    }


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
) -> None:
    """List configured providers and available adapters (section 43)."""
    from ..providers.registry import PROVIDER_REGISTRY

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
        console.print(f"  adapter    {spec.adapter}")
        console.print(f"  url        {spec.base_url or '-'}")
        console.print(f"  model      {spec.model or '-'}")
        console.print(f"  secret id  {spec.secret or '-'}")


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
