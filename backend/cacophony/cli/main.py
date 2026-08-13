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
from ..core.provenance import ProvenanceMode
from ..core.record import to_jsonable
from ..generation.engine import DEFAULT_BATCH_SIZE, FailurePolicy, GenerationEngine
from ..generation.registry import REGISTRY
from ..generation.runtime import GenerationRuntime
from ..outputs import OUTPUT_FORMATS, create_writer, output_path_for
from ..providers.cache import CacheMode, GenerationCache
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


CacheOpt = Annotated[
    str, typer.Option("--cache", help=f"One of: {', '.join(m.value for m in CacheMode)}.")
]
CachePathOpt = Annotated[
    Path | None, typer.Option("--cache-path", help="Where to keep the generation cache.")
]


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
    llm_batch_size: Annotated[
        int,
        typer.Option("--llm-batch-size", help="Records per language-model call in batch mode."),
    ] = 20,
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
        runtime=_runtime(
            compiled,
            cache_mode=cache,
            cache_path=cache_path,
            llm_batch_size=llm_batch_size,
        ),
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
    _print_provider_activity(engine)
    asyncio.run(engine.aclose())


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


def _print_provider_activity(engine: GenerationEngine) -> None:
    """Report what the providers actually did (sections 58, 86)."""
    runtime = getattr(engine, "runtime", None)
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
