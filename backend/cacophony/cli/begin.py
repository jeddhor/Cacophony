"""One sentence to a world (design document section 110).

Section 110 is the design document's closing image, and it is written as a
user's experience rather than as a feature:

    The user says: "I need fake data for a multinational retail company with
    25,000 employees, 200 stores, 18 months of sales transactions, realistic IT
    activity, customer support tickets, employee headshots, voice recordings
    from a call center, and a few deliberately planted cybersecurity
    incidents."

    Cacophony constructs a proposed synthetic world. The user edits the plan.
    Then presses: BEGIN CACOPHONY.

Every part of that already existed - ``propose`` asks the model, the Studio
edits the result, ``generate`` runs it - and what was missing was the flow that
joins them. This is that flow, and it is deliberately thin: it proposes, shows
the world it proposes to build, lets you edit it, asks once, and hands the
compiled project to the same Conductor ``cacophony generate`` uses. Nothing here
generates a record itself.

Two things it insists on, because the alternative is a party trick rather than a
tool. The schema is **written to a file** before anything is generated, so what
comes out is reproducible by somebody who was not here. And it **asks** before
starting, because a sentence can quite easily describe four hours of work and a
hundred gigabytes.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from ..core.errors import CacophonyError
from .proposing import ask_for_a_schema, provider_spec_for
from .runs import build_run_config, drive, register_project, report_outcome
from .theme import console, error_console

__all__ = ["register"]


def _slug(name: str) -> str:
    """A filename for a world that has only ever had a description."""
    text = unicodedata.normalize("NFKD", name)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-") or "world"


def _describe_world(compiled: Any) -> None:
    """Print what is about to be built, in the order somebody would ask.

    Not the full plan - that is what ``cacophony plan`` is for - but the four
    numbers that decide whether you want to press the button: how many records,
    how many model calls, how much media, and how much disk.
    """
    # Imported here rather than at the top: main imports this module at the end
    # of its own body to register the command, so a module-level import back
    # into it would be a cycle waiting for somebody to reorder something.
    from .main import _human_bytes

    generation_plan = compiled.plan
    assert generation_plan is not None
    estimate = generation_plan.estimate

    console.print(
        f"[cacophony.ok]proposed[/] {len(compiled.entity_order)} entities, "
        f"{estimate.records:,} records"
    )
    for step in generation_plan.steps:
        after = (
            f"  [cacophony.muted]after {', '.join(step.depends_on)}[/]" if step.depends_on else ""
        )
        console.print(
            f"  [cacophony.accent]{step.entity:<24}[/]"
            f"[cacophony.muted]{step.count:>12,} records[/]{after}"
        )

    console.print()
    console.print(f"  [cacophony.muted]field values     {estimate.fields:,}[/]")
    if estimate.llm_calls:
        console.print(f"  [cacophony.muted]language-model   {estimate.llm_calls:,} calls[/]")
    if estimate.image_calls:
        console.print(f"  [cacophony.muted]images           {estimate.image_calls:,}[/]")
    if estimate.speech_calls:
        console.print(f"  [cacophony.muted]audio            {estimate.speech_calls:,}[/]")
    storage = _human_bytes(estimate.estimated_bytes)
    console.print(f"  [cacophony.muted]storage (approx) {storage}[/]")

    if generation_plan.warnings:
        console.print()
        for warning in generation_plan.warnings:
            console.print(f"  [cacophony.warn]note[/] {warning}")


def _lint_summary(compiled: Any) -> bool:
    """Show what the linter thinks. Returns False if it found an error.

    A proposal that lints clean is the normal case, because the assistant
    already lints its own output and retries. This is here for what happens
    after *you* edit it.
    """
    from ..schema.linter import Severity, lint_project

    issues = lint_project(compiled)
    if not len(issues):
        return True

    console.print()
    console.print(issues.render())
    return not any(issue.severity is Severity.ERROR for issue in issues)


def register(app: typer.Typer) -> None:
    """Attach the ``begin`` command."""

    @app.command()
    def begin(
        description: Annotated[str, typer.Argument(help="The world you want, in plain language.")],
        out: Annotated[
            Path | None,
            typer.Option("--out", "-o", help="Where to write the schema. Default: named after it."),
        ] = None,
        out_dir: Annotated[
            Path, typer.Option("--out-dir", "-d", help="Where the data goes.")
        ] = Path("out"),
        fmt: Annotated[
            str, typer.Option("--format", "-f", help="Output format for the data.")
        ] = "jsonl",
        scale: Annotated[
            int | None, typer.Option("--scale", help="Divide every proposed record count by this.")
        ] = None,
        seed: Annotated[int | None, typer.Option("--seed", help="Seed the world.")] = None,
        edit: Annotated[
            bool, typer.Option("--edit", "-e", help="Open the schema in $EDITOR before deciding.")
        ] = False,
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask; begin as soon as it compiles.")
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite the schema file if it exists.")
        ] = False,
        provider_from: Annotated[
            Path | None,
            typer.Option(
                "--providers", help="A project file whose provider configuration to borrow."
            ),
        ] = None,
        adapter: Annotated[
            str, typer.Option("--adapter", help="Provider adapter to use when none is borrowed.")
        ] = "ollama",
        base_url: Annotated[str | None, typer.Option("--url", help="Provider base URL.")] = None,
        model: Annotated[str | None, typer.Option("--model", "-m", help="Model to ask.")] = None,
        store: Annotated[
            Path | None, typer.Option("--store", help="Path to the run store (SQLite).")
        ] = None,
    ) -> None:
        """Describe a world, then build it (design document section 110).

            cacophony begin "a small hospital: staff, wards, admissions over a year"

        Proposes a schema from the description, shows you what it would produce,
        lets you edit it, and generates it. The schema is written to a file
        first, so the result is a project you can run again rather than a pile
        of records nobody can reproduce.
        """
        from ..observability.logging import configure_logging
        from ..runs.coordinator import Conductor
        from ..schema.compiler import compile_project
        from ..schema.loader import load_project

        configure_logging("warning")

        console.print()
        console.rule("[cacophony.brand]CACOPHONY[/]", style="cacophony.rule")
        console.print(f"[cacophony.muted]building[/] {description.strip()}")
        console.print()

        spec = provider_spec_for(
            provider_from=provider_from, adapter=adapter, base_url=base_url, model=model
        )
        proposal = ask_for_a_schema(spec, description, seed=seed, scale=scale)

        # The schema is written before anything is generated. A world that only
        # ever existed in one terminal session is not a synthetic world; it is
        # an anecdote.
        proposed_name = str(proposal.data.get("project", {}).get("name") or description)
        target = out or Path(f"{_slug(proposed_name)}.yaml")
        if target.exists() and not force:
            error_console.print(
                f"[cacophony.error]error[/] {target} already exists. "
                "Pass --out to write somewhere else, or --force to overwrite it."
            )
            raise typer.Exit(code=2)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal.yaml, encoding="utf-8")
        console.print(f"[cacophony.ok]schema[/] {target}")
        for note in proposal.notes:
            console.print(f"  [cacophony.warn]note[/] {note}")

        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if edit and interactive:
            click.edit(filename=str(target))

        while True:
            try:
                compiled = compile_project(load_project(target))
            except CacophonyError as exc:
                error_console.print(f"[cacophony.error]error[/] {exc}")
                if not interactive:
                    raise typer.Exit(code=2) from exc
                if not typer.confirm("The schema does not compile. Edit it?", default=True):
                    raise typer.Exit(code=2) from exc
                click.edit(filename=str(target))
                continue

            console.print()
            _describe_world(compiled)
            clean = _lint_summary(compiled)

            if yes or not interactive:
                if not clean:
                    error_console.print(
                        "\n[cacophony.error]error[/] the schema has lint errors and nothing "
                        "is here to fix them. Run it again in a terminal, or edit the file."
                    )
                    raise typer.Exit(code=1)
                break

            console.print()
            answer = (
                typer.prompt(
                    "BEGIN CACOPHONY? [b]egin, [e]dit, [q]uit", default="b" if clean else "e"
                )
                .strip()
                .lower()
            )
            if answer.startswith("q"):
                console.print(f"[cacophony.muted]left as it is:[/] {target}")
                raise typer.Exit(code=0)
            if answer.startswith("e"):
                click.edit(filename=str(target))
                continue
            if not clean and not typer.confirm("The linter found errors. Begin anyway?"):
                continue
            break

        config = build_run_config(
            out_dir=out_dir,
            output=fmt,
            entity=None,
            records=None,
            seed=seed,
            no_validate=False,
            drop_invalid=False,
            provenance="none",
            on_failure="abort",
            cache="disabled",
            cache_path=None,
            batch_size=1000,
            workers=4,
            llm_batch_size=20,
            checkpoint_every=10_000,
            record_history=True,
        )
        repository, project_id, revision_id = register_project(target, compiled, store, config)

        console.print()
        console.rule("[cacophony.brand]BEGIN CACOPHONY[/]", style="cacophony.rule")
        console.print(
            f"[cacophony.muted]seed[/] {compiled.seed}   "
            f"[cacophony.muted]format[/] {config.output_format}   "
            f"[cacophony.muted]output[/] {config.output_dir.resolve()}"
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
        if outcome.ok:
            console.print(f"\n[cacophony.muted]the world is in[/] {config.output_dir.resolve()}")
            console.print(f"[cacophony.muted]run it again with[/] cacophony generate {target}")
        report_outcome(outcome)
