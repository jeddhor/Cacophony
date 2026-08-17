"""Helpers shared by the run-oriented CLI commands (sections 32, 55, 56, 73).

Kept out of ``main.py`` because these are the parts with real behaviour -
resolving a store, recovering the schema a run used, driving the progress
display - as opposed to argument parsing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ..core.provenance import ProvenanceMode
from ..generation.engine import FailurePolicy
from ..outputs import OUTPUT_FORMATS
from ..providers.cache import CacheMode
from ..runs.config import ResourceLimits, RunConfig
from ..runs.coordinator import Conductor, RunOutcome
from ..runs.events import EventKind
from ..runs.state import RunState
from ..schema.compiler import compile_project
from ..schema.loader import load_project, load_project_data
from ..store.database import Database, default_store_path
from ..store.repository import Repository
from .theme import console, error_console

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledProject

__all__ = [
    "STATE_STYLES",
    "build_run_config",
    "compile_stored_revision",
    "drive",
    "open_repository",
    "pick_run",
    "register_project",
]

STATE_STYLES = {
    "completed": "cacophony.ok",
    "running": "cacophony.accent",
    "queued": "cacophony.muted",
    "paused": "cacophony.warn",
    "retrying": "cacophony.warn",
    "failed": "cacophony.error",
    "cancelled": "cacophony.warn",
}


def _bad_choice(what: str, given: str, allowed: Any) -> typer.Exit:
    error_console.print(
        f"[cacophony.error]error[/] unknown {what} '{given}'. "
        f"Choose one of: {', '.join(sorted(allowed))}"
    )
    return typer.Exit(code=2)


def build_run_config(
    *,
    out_dir: Path,
    output: str,
    entity: str | None,
    records: int | None,
    seed: int | None,
    no_validate: bool,
    drop_invalid: bool,
    provenance: str,
    on_failure: str,
    cache: str,
    cache_path: Path | None,
    batch_size: int,
    workers: int,
    llm_batch_size: int,
    checkpoint_every: int,
    record_history: bool,
    assets_dir: Path | None = None,
    overwrite_assets: bool = False,
    edge_cases: float = 0.0,
    edge_categories: str | None = None,
) -> RunConfig:
    """Turn command-line options into a run configuration, or exit clearly."""
    try:
        provenance_mode = ProvenanceMode(provenance)
    except ValueError as exc:
        raise _bad_choice(
            "provenance mode", provenance, [mode.value for mode in ProvenanceMode]
        ) from exc

    try:
        cache_mode = CacheMode(cache)
    except ValueError as exc:
        raise _bad_choice("cache mode", cache, [mode.value for mode in CacheMode]) from exc

    if output.lower() not in OUTPUT_FORMATS:
        raise _bad_choice("output format", output, OUTPUT_FORMATS)
    if on_failure not in FailurePolicy.ALL:
        raise _bad_choice("failure policy", on_failure, FailurePolicy.ALL)

    return RunConfig(
        output_dir=out_dir,
        output_format=output,
        assets_dir=assets_dir,
        overwrite_assets=overwrite_assets,
        entities=[entity] if entity else [],
        records=records,
        seed=seed,
        validate=not no_validate,
        drop_invalid=drop_invalid,
        edge_cases=edge_cases,
        edge_categories=_edge_categories(edge_categories),
        provenance=provenance_mode,
        failure_policy=on_failure,
        cache_mode=cache_mode,
        cache_path=cache_path,
        checkpoint_every=checkpoint_every,
        record_history=record_history,
        limits=ResourceLimits(
            max_workers=max(1, workers),
            batch_size=max(1, batch_size),
            llm_batch_size=max(1, llm_batch_size),
        ),
    )


def _edge_categories(value: str | None) -> list[str]:
    """Parse ``--edge-categories emoji,rtl_text``, refusing an unknown name.

    Refused rather than ignored: a typo that silently produced no edge cases
    would look exactly like an application with no bugs.
    """
    from ..simulation.edges import CATEGORIES

    if not value:
        return []
    wanted = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in CATEGORIES]
    if unknown:
        raise _bad_choice("edge-case category", ", ".join(unknown), CATEGORIES)
    return wanted


def open_repository(store: Path | None, project: Path | None = None) -> Repository | None:
    """Open the run store, or report that there is not one yet."""
    path = store or default_store_path(project)
    if store is None and not Path(path).exists():
        return None
    try:
        return Repository(Database(path))
    except RuntimeError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        raise typer.Exit(code=2) from exc


def register_project(
    project_path: Path, compiled: CompiledProject, store: Path | None, config: RunConfig
) -> tuple[Repository | None, int | None, int | None]:
    """Record the project and its schema revision before a run starts.

    A store that cannot be opened costs the history, not the run: generation is
    the point, and history is bookkeeping about it.
    """
    if not config.record_history:
        return None, None, None
    path = store or default_store_path(project_path)
    try:
        repository = Repository(Database(path))
        project_id, revision_id = repository.upsert_project(
            compiled.spec,
            path=project_path,
            source_text=Path(project_path).read_text(encoding="utf-8"),
            source_format="json" if project_path.suffix.lower() == ".json" else "yaml",
        )
    except (OSError, RuntimeError) as exc:
        error_console.print(f"[cacophony.warn]warning[/] run history unavailable: {exc}")
        return None, None, None
    return repository, project_id, revision_id


def pick_run(repository: Repository, run: str | None) -> dict[str, Any]:
    """Resolve a run id, a unique prefix of one, or the latest resumable run."""
    if run is None:
        candidates = repository.resumable_runs()
        if not candidates:
            error_console.print("[cacophony.error]error[/] no resumable run found")
            raise typer.Exit(code=2)
        stored = repository.get_run(candidates[0]["id"])
        assert stored is not None
        return stored

    stored = repository.get_run(run)
    if stored is not None:
        return stored

    matches = [row for row in repository.list_runs(limit=500) if row["id"].startswith(run)]
    if len(matches) == 1:
        found = repository.get_run(matches[0]["id"])
        assert found is not None
        return found
    if not matches:
        error_console.print(f"[cacophony.error]error[/] no run matching '{run}'")
    else:
        error_console.print(
            f"[cacophony.error]error[/] '{run}' matches {len(matches)} runs: "
            + ", ".join(match["id"][:8] for match in matches[:5])
        )
    raise typer.Exit(code=2)


def compile_stored_revision(repository: Repository, stored: dict[str, Any]) -> CompiledProject:
    """Recompile the exact schema revision a run used (design document section 73).

    Resuming against whatever the file says *now* would silently produce a
    dataset generated from two different schemas. The revision is stored
    precisely so that cannot happen.
    """
    revision_id = stored.get("revision_id")
    if revision_id is not None:
        revision = repository.get_revision(revision_id, include_source=True)
        if revision is not None:
            import yaml

            data = (
                json.loads(revision["source_text"])
                if revision["source_format"] == "json"
                else yaml.safe_load(revision["source_text"])
            )
            return compile_project(load_project_data(data, source=f"revision {revision_id}"))

    record = repository.get_project(stored["project_id"])
    if record and record.get("path") and Path(record["path"]).exists():
        error_console.print(
            "[cacophony.warn]warning[/] this run recorded no schema revision; "
            "using the project file as it stands now"
        )
        return compile_project(load_project(record["path"]))

    error_console.print("[cacophony.error]error[/] the schema this run used is no longer available")
    raise typer.Exit(code=2)


def drive(conductor: Conductor, *, resume: bool = False) -> RunOutcome:
    """Execute a run, showing section 55's live progress while it goes."""
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    progress = Progress(
        SpinnerColumn(style="cacophony.highlight"),
        TextColumn("[cacophony.accent]{task.description}"),
        BarColumn(complete_style="cacophony.brand", finished_style="cacophony.ok"),
        MofNCompleteColumn(),
        TextColumn("[cacophony.muted]{task.fields[rate]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    tasks: dict[str, Any] = {}

    def on_event(event: Any) -> None:
        if event.kind is EventKind.JOB_STARTED and event.entity:
            tasks[event.entity] = progress.add_task(
                event.entity,
                total=event.data.get("requested", 0),
                completed=event.data.get("completed", 0),
                rate="",
            )
        elif event.kind is EventKind.JOB_PROGRESS and event.entity in tasks:
            per_entity = event.data.get("entities", {}).get(event.entity, {})
            progress.update(
                tasks[event.entity],
                completed=event.data.get("completed", 0),
                rate=f"{per_entity.get('records_per_second', 0):,.0f}/s",
            )
        elif event.kind is EventKind.JOB_COMPLETED and event.entity in tasks:
            progress.update(tasks[event.entity], completed=event.data.get("records", 0))
        elif event.kind is EventKind.WARNING:
            progress.console.print(f"[cacophony.warn]warning[/] {event.message}")

    remove = conductor.bus.add_sink(on_event)
    try:
        with progress:
            return asyncio.run(_execute(conductor, resume=resume))
    except KeyboardInterrupt:
        # Ctrl-C is a pause, not a loss: the job checkpoints on the way out.
        console.print("\n[cacophony.warn]interrupted[/] - the run is checkpointed")
        console.print(
            f"[cacophony.muted]resume it with:[/] cacophony resume {conductor.run_id[:8]}"
        )
        raise typer.Exit(code=130) from None
    finally:
        remove()


async def _execute(conductor: Conductor, *, resume: bool) -> RunOutcome:
    try:
        return await (conductor.execute_resume() if resume else conductor.execute())
    finally:
        await conductor.aclose()


def _report_relations(summary: dict[str, Any]) -> None:
    """The two scores a relational run earns (design document section 58).

    Only printed when the run had something to say: a project with no
    references and no declared distributions has no referential integrity to
    report, and a row reading "100%" for a check nobody ran is a lie told
    tidily.
    """
    quality = summary.get("quality") or {}

    integrity = quality.get("referential_integrity")
    if integrity is not None:
        checked = sum(
            int((entity.get("referential") or {}).get("references_checked", 0))
            for entity in (summary.get("validation") or {}).values()
        )
        style = "cacophony.ok" if integrity >= 1.0 else "cacophony.warn"
        console.print(
            f"  [{style}]referential     {integrity:.2%}[/]"
            f"  [cacophony.muted]({checked:,} references checked)[/]"
        )

    match = quality.get("distribution_match")
    if match is not None:
        style = "cacophony.ok" if match >= 0.92 else "cacophony.warn"
        console.print(f"  [{style}]distributions   {match:.2%}[/] [cacophony.muted]match[/]")

    relations = summary.get("relations")
    if relations:
        console.print(
            f"  [cacophony.muted]references      {relations['key_lookups']:,} resolved, "
            f"{relations['key_hit_rate']:.0%} from cache[/]"
        )


def _report_assets(summary: dict[str, Any]) -> None:
    """What a multimodal run produced (design document sections 19, 81)."""
    assets = summary.get("assets")
    if not assets or not assets.get("assets"):
        return

    console.print(
        f"  assets          {assets['assets']:,} files, {_human_size(assets['bytes_written'])}"
    )
    saved = assets.get("deduplicated", 0) + assets.get("reused_from_disk", 0)
    if saved:
        console.print(
            f"  [cacophony.muted]                {assets.get('deduplicated', 0):,} deduplicated, "
            f"{assets.get('reused_from_disk', 0):,} already on disk[/]"
        )
    console.print(f"  [cacophony.muted]                {assets['root']}[/]")


def _human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


def _report_world(summary: dict[str, Any]) -> None:
    """What the simulation and the chaos controls did (sections 17, 24, 25)."""
    scenarios = summary.get("scenarios")
    if scenarios and scenarios.get("records_affected"):
        counts = scenarios.get("by_scenario") or {}
        listed = ", ".join(
            f"{name} {count:,}" for name, count in sorted(counts.items(), key=lambda p: -p[1])
        )
        console.print(f"  scenarios       {scenarios['records_affected']:,} records affected")
        if listed:
            console.print(f"  [cacophony.muted]                {listed}[/]")

    simulation = summary.get("simulation") or {}
    for name, detail in simulation.items():
        allocation = detail.get("allocation") or {}
        line = (
            f"  simulation      {name}: {allocation.get('events', 0):,} events over "
            f"{allocation.get('subjects', 0):,} {detail.get('subject', 'subjects')}"
        )
        console.print(line)
        state = detail.get("state")
        if state and state.get("events_folded"):
            console.print(
                f"  [cacophony.muted]                state {', '.join(state['variables'])}; "
                f"{state['events_folded']:,} folded, {state['replayed']:,} replayed[/]"
            )

    chaos = summary.get("chaos") or {}
    damaged = sum(entry.get("records_damaged", 0) for entry in chaos.values())
    duplicates = sum(entry.get("duplicates_emitted", 0) for entry in chaos.values())
    if damaged or duplicates:
        kinds: dict[str, int] = {}
        for entry in chaos.values():
            for kind, count in (entry.get("by_kind") or {}).items():
                kinds[kind] = kinds.get(kind, 0) + count
        console.print(
            f"  [cacophony.warn]chaos           {damaged:,} records damaged, "
            f"{duplicates:,} duplicated[/]"
        )
        console.print(
            "  [cacophony.muted]                "
            + ", ".join(f"{kind} {count:,}" for kind, count in sorted(kinds.items()))
            + "[/]"
        )


def _report_duplication(summary: dict[str, Any]) -> None:
    """What the model repeated (design document section 59).

    The rate, what it was measured over, and - when the news is bad - one of
    the repeated values, because "2.4% near duplicates" is an argument and a
    quoted paragraph is evidence.
    """
    reports = summary.get("duplication") or {}
    if not reports:
        return

    for name, report in reports.items():
        values = int(report.get("checked_values", 0))
        if not values:
            continue
        exact = int(report.get("exact", 0)) + int(report.get("normalized", 0))
        near = int(report.get("near", 0))
        unique = float(report.get("uniqueness", 1.0))
        style = "cacophony.ok" if report.get("ok", True) and unique >= 0.98 else "cacophony.warn"

        console.print(
            f"  [{style}]duplication     {name}: {unique:.2%} unique[/]"
            f"  [cacophony.muted]({exact:,} repeated, {near:,} near, "
            f"over {values:,} values)[/]"
        )
        fields = report.get("fields") or []
        if fields:
            console.print(
                f"  [cacophony.muted]                compared {', '.join(fields)}"
                f" by {', '.join(report.get('methods') or [])}[/]"
            )

        # A Bloom filter has false positives, and a figure computed from one
        # has to say so rather than presenting an estimate as a count.
        bloom = report.get("bloom") or {}
        rate = float(bloom.get("false_positive_rate", 0.0))
        if exact and rate > 0:
            console.print(
                f"  [cacophony.muted]                up to {rate:.3%} of the exact matches "
                "may be filter false positives[/]"
            )

        for breach in report.get("breaches") or []:
            error_console.print(f"  [cacophony.error]                {breach}[/]")

        examples = report.get("examples") or []
        if examples and not report.get("ok", True):
            first = examples[0]
            console.print(
                f"  [cacophony.muted]                e.g. {first['field']} at record "
                f"{first['record_index']}: {first['excerpt']}[/]"
            )


def _report_edge_cases(summary: dict[str, Any]) -> None:
    """What was made deliberately awkward (design document section 79).

    Reported separately from chaos, and worded differently, because the two
    findings mean opposite things. A pipeline that chokes on chaos is working;
    a pipeline that chokes on one of these has a bug.
    """
    reports = summary.get("edge_cases") or {}
    if not reports:
        return

    for name, report in reports.items():
        marked = int(report.get("records_marked", 0))
        if not marked:
            continue
        categories = report.get("by_category") or {}
        console.print(
            f"  edge cases      {name}: {marked:,} records carry a legal but awkward value "
            f"[cacophony.muted]({report.get('rate', 0):.1%})[/]"
        )
        if categories:
            console.print(
                "  [cacophony.muted]                "
                + ", ".join(f"{kind} {count:,}" for kind, count in sorted(categories.items()))
                + "[/]"
            )
        rejected = int(report.get("rejected_as_invalid", 0))
        if rejected:
            # An edge case the schema would not accept is not an edge case.
            console.print(
                f"  [cacophony.muted]                {rejected:,} candidates were dropped: "
                "the field could not legally hold them[/]"
            )


def _report_patches(summary: dict[str, Any]) -> None:
    """What the project's patch rules did (design document section 104).

    Reported because a patched column looks exactly like a column that was
    always that way. Somebody reading the run later needs to know that thirteen
    addresses are masked because the schema says so.
    """
    reports = summary.get("patches") or {}
    if not reports:
        return

    for name, report in reports.items():
        edited = int(report.get("records_edited", 0))
        dropped = int(report.get("records_dropped", 0))
        if not edited and not dropped:
            continue
        parts = []
        if edited:
            parts.append(f"{edited:,} records edited ({report.get('values_changed', 0):,} values)")
        if dropped:
            parts.append(f"{dropped:,} dropped")
        console.print(f"  patches         {name}: {', '.join(parts)}")
        for rule in report.get("rules") or []:
            console.print(f"  [cacophony.muted]                {rule['name']}[/]")


def report_outcome(outcome: RunOutcome, *, on_provider_activity: Any = None) -> None:
    """Print what happened, and exit non-zero if it was not what was asked."""
    console.rule(style="cacophony.rule")
    summary = outcome.summary

    if outcome.ok:
        console.print(
            f"[cacophony.ok]complete[/]  {outcome.records:,} records in "
            f"{outcome.duration_seconds:.2f}s"
        )
    else:
        style = "cacophony.warn" if outcome.state is RunState.CANCELLED else "cacophony.error"
        console.print(f"[{style}]{outcome.state.value}[/]  {outcome.records:,} records written")
        if outcome.error:
            error_console.print(f"[cacophony.error]error[/] {outcome.error}")

    if outcome.duration_seconds > 0:
        console.print(
            f"  throughput      {outcome.records / outcome.duration_seconds:,.0f} records/sec"
        )
    console.print(f"  files           {len(set(outcome.files))}")
    console.print(f"  run             {outcome.run_id}")

    failures = int(summary.get("validation_failures", 0))
    if failures:
        console.print(f"  [cacophony.warn]validation failures  {failures:,}[/]")

    _report_relations(summary)
    _report_duplication(summary)
    _report_assets(summary)
    _report_world(summary)
    _report_edge_cases(summary)
    _report_patches(summary)

    if on_provider_activity is not None:
        on_provider_activity()

    if not outcome.ok:
        console.print(f"\n[cacophony.muted]resume with:[/] cacophony resume {outcome.run_id[:8]}")
        raise typer.Exit(code=3 if outcome.state is RunState.FAILED else 4)
