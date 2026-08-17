"""``cacophony transform`` and ``cacophony regenerate`` (sections 104, 105).

    cacophony transform out/employee.jsonl --set 'email = mask:4' -o masked.jsonl
    cacophony regenerate project.yaml -e employee --records 4823913-4823920

Two ways of dealing with a dataset that already exists and is not quite right.

``transform`` rewrites a file, streaming. ``regenerate`` does not touch the file
at all - it re-derives the records from the schema, which costs nothing because a
record's seed is a hash of its position (section 75). "Row 4,823,913 looks
wrong" therefore has an answer that takes milliseconds and needs no run, no
checkpoint and no copy of the dataset.

Both print the same warning when a transform is not recorded as a rule, because
a dataset edited outside its schema has stopped being a function of it - and the
next ``generate`` will overwrite the edit without noticing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from ..core.errors import CacophonyError
from ..core.record import to_jsonable
from .theme import console, error_console

__all__ = ["register"]


def _parse_set(values: list[str]) -> dict[str, Any]:
    """Read ``--set 'email = mask:4'`` arguments into a ``set:`` block."""
    block: dict[str, Any] = {}
    for item in values:
        field_name, separator, expression = item.partition("=")
        if not separator:
            raise CacophonyError(
                f"--set needs FIELD=OPERATION, e.g. --set 'email=mask:4'. Got: {item!r}"
            )
        name = field_name.strip()
        if not name:
            raise CacophonyError(f"--set needs a field name before the '='. Got: {item!r}")
        block[name] = expression.strip()
    return block


def _parse_range(value: str) -> tuple[int, int]:
    """Read ``4823913-4823920`` or ``4823913`` into a half-open range."""
    text = value.strip()
    if "-" in text:
        start_text, _, end_text = text.partition("-")
        try:
            start, end = int(start_text), int(end_text)
        except ValueError as exc:
            raise CacophonyError(f"--records needs N or N-M, got {value!r}") from exc
        if end < start:
            raise CacophonyError(f"--records {value!r} ends before it starts")
        return start, end + 1
    try:
        index = int(text)
    except ValueError as exc:
        raise CacophonyError(f"--records needs N or N-M, got {value!r}") from exc
    return index, index + 1


def register(app: typer.Typer) -> None:
    """Attach ``transform`` and ``regenerate``."""

    @app.command()
    def transform(
        source: Path = typer.Argument(..., help="Dataset file to transform."),
        set_: list[str] = typer.Option(
            [],
            "--set",
            "-s",
            help="FIELD=OPERATION, e.g. --set 'email=mask:4'. Repeatable.",
        ),
        where: str | None = typer.Option(
            None, "--where", "-w", help="Only records matching this expression."
        ),
        drop_where: str | None = typer.Option(
            None, "--drop-where", help="Drop records matching this expression."
        ),
        keep_where: str | None = typer.Option(
            None, "--keep-where", help="Keep only records matching this expression."
        ),
        out: Path | None = typer.Option(None, "--out", "-o", help="Where to write the result."),
        in_place: bool = typer.Option(
            False, "--in-place", help="Rewrite the file, via a temporary beside it."
        ),
        fmt: str | None = typer.Option(
            None, "--format", "-f", help="jsonl, csv or json. Default: from the suffix."
        ),
        project: Path | None = typer.Option(
            None,
            "--project",
            "-p",
            help="Apply the project's own patch rules instead of the flags.",
        ),
        entity: str | None = typer.Option(
            None, "--entity", "-e", help="Which entity's rules to apply."
        ),
        record_as: str | None = typer.Option(
            None,
            "--record-as",
            help="Also print this transform as a patch rule to paste into the project.",
        ),
        force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Apply transformations to a dataset that already exists (section 105).

            cacophony transform out/employee.jsonl \\
                --set 'email=mask:4' --where "department == 'Finance'" \\
                -o out/employee.masked.jsonl

        Streams, so the file size does not matter. Never writes over its input
        until the new file is complete.
        """
        from ..transforms import transform_file
        from .main import _banner

        try:
            rules = _rules_for(project, entity, set_, where, drop_where, keep_where)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if not rules:
            error_console.print(
                "[cacophony.error]error[/] nothing to do. Give --set, --drop-where, "
                "--keep-where, or --project to use its patch rules."
            )
            raise typer.Exit(code=2)

        try:
            result = transform_file(
                source,
                rules,
                destination=out,
                fmt=fmt,
                in_place=in_place,
                overwrite=force,
                entity=entity or "",
            )
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if as_json:
            console.print_json(json.dumps(result.to_dict()))
            return

        _banner("transform", str(source))
        for rule in rules:
            console.print(f"[cacophony.muted]rule  [/] {rule.describe()}")
        console.print()
        console.print(f"[cacophony.ok]wrote[/] {result.destination}")
        console.print(f"  read            {result.read:,}")
        console.print(f"  written         {result.written:,}")
        console.print(f"  edited          {result.edited:,}  ({result.values_changed:,} values)")
        if result.dropped:
            console.print(f"  [cacophony.warn]dropped         {result.dropped:,}[/]")
        if result.sidecar:
            console.print(f"  [cacophony.muted]recorded in     {result.sidecar.name}[/]")

        if record_as:
            _print_rule(record_as, rules)
        elif project is None:
            # The warning that matters. A file edited outside its schema has
            # stopped being a function of it, and the next `generate` will
            # overwrite the edit without noticing.
            console.print(
                "\n[cacophony.warn]note[/] this changed a file, not the project. The next "
                "'cacophony generate' will produce the untransformed data again. Pass "
                "--record-as NAME to get a patch rule that makes the change part of the "
                "schema (design document section 104)."
            )

    def _rules_for(
        project: Path | None,
        entity: str | None,
        set_: list[str],
        where: str | None,
        drop_where: str | None,
        keep_where: str | None,
    ) -> list[Any]:
        """Either the project's rules or the ones spelled out on the command line."""
        from ..transforms import PatchRule

        if project is not None:
            if set_ or where or drop_where or keep_where:
                raise CacophonyError(
                    "--project applies the project's own patch rules; it cannot be combined "
                    "with --set or a filter. Drop one."
                )
            from ..schema.loader import load_project

            spec = load_project(project)
            if not spec.patches:
                raise CacophonyError(f"{project} declares no 'patches:' block")
            return [
                PatchRule.from_spec(name, patch)
                for name, patch in spec.patches.items()
                if not entity or not patch.entity or patch.entity == entity
            ]

        if sum(1 for flag in (drop_where, keep_where) if flag) > 1:
            raise CacophonyError("pass either --drop-where or --keep-where, not both")

        rules: list[Any] = []
        if set_:
            rules.append(
                PatchRule.parse(
                    "cli",
                    {
                        "entity": entity or "",
                        "where": where,
                        "set": _parse_set(set_),
                    },
                )
            )
        if drop_where:
            rules.append(PatchRule.parse("cli-drop", {"where": drop_where, "drop": True}))
        if keep_where:
            rules.append(PatchRule.parse("cli-keep", {"where": keep_where, "keep": True}))
        return rules

    def _print_rule(name: str, rules: list[Any]) -> None:
        """Print the equivalent ``patches:`` block, ready to paste."""
        import yaml

        block: dict[str, Any] = {}
        for index, rule in enumerate(rules):
            key = name if len(rules) == 1 else f"{name}_{index + 1}"
            entry: dict[str, Any] = {}
            if rule.entity:
                entry["entity"] = rule.entity
            if rule.condition is not None:
                entry["where"] = rule.condition.source
            if rule.drop:
                entry["drop"] = True
            elif rule.keep:
                entry["keep"] = True
            else:
                entry["set"] = {
                    edit.field: (
                        " | ".join(f"{op}:{arg}" if arg else op for op, arg in edit.operations)
                        if edit.operations
                        else (
                            edit.expression.source
                            if edit.expression is not None
                            else {"value": edit.value}
                        )
                    )
                    for edit in rule.edits
                }
            block[key] = entry

        console.print("\n[cacophony.accent]as a patch rule[/]")
        console.print(
            "[cacophony.muted]paste this into the project, and the change survives a "
            "regeneration[/]\n"
        )
        text = yaml.safe_dump({"patches": block}, sort_keys=False, allow_unicode=True)
        for line in text.splitlines():
            console.print(f"  {line}")

    # -- regenerate ----------------------------------------------------------- #

    @app.command()
    def regenerate(
        project: Path = typer.Argument(..., help="Path to a project YAML or JSON file."),
        entity: str | None = typer.Option(
            None, "--entity", "-e", help="Which entity. Default: the first one."
        ),
        records: str = typer.Option(
            ..., "--records", "-r", help="An index or a range: 4823913 or 4823913-4823920."
        ),
        columns: str | None = typer.Option(
            None, "--columns", "-c", help="Comma-separated columns to show."
        ),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
        seed: int | None = typer.Option(None, "--seed", help="Override the project seed."),
    ) -> None:
        """Re-derive specific records, without a run (sections 75, 104).

            cacophony regenerate project.yaml -e employee -r 4823913-4823920

        Costs nothing and needs nothing: a record's seed is a hash of its
        position, so record 4,823,913 can be produced on its own without the
        4,823,912 before it, without the dataset, and without the run that
        made it. "This row looks wrong" is a question with a cheap answer.
        """
        import asyncio

        from ..generation.engine import GenerationEngine
        from .main import _banner, _load, _truncate

        compiled = _load(project, seed)
        target = entity or (compiled.entity_order[0] if compiled.entity_order else "")
        if target not in compiled.entities:
            error_console.print(
                f"[cacophony.error]error[/] no entity '{target}'. "
                f"Known: {', '.join(compiled.entity_order)}"
            )
            raise typer.Exit(code=2)

        try:
            start, end = _parse_range(records)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        count = end - start
        if count > 1000:
            error_console.print(
                f"[cacophony.error]error[/] {count:,} records is a generate, not a "
                "regenerate. Use 'cacophony generate' with --records."
            )
            raise typer.Exit(code=2)

        engine = GenerationEngine(compiled)
        try:
            produced = asyncio.run(engine.generate_batch(target, count, offset=start))
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if as_json:
            console.print_json(
                json.dumps(
                    {
                        "entity": target,
                        "seed": compiled.seed,
                        "records": [
                            {"index": start + offset, **to_jsonable(record.values)}
                            for offset, record in enumerate(produced)
                        ],
                    },
                    default=str,
                )
            )
            return

        from rich.table import Table

        wanted = (
            [name.strip() for name in columns.split(",") if name.strip()]
            if columns
            else compiled.entity(target).spec.field_names()
        )

        _banner("regenerate", f"{compiled.name} · {target}")
        console.print(
            f"[cacophony.muted]seed[/] {compiled.seed}   "
            f"[cacophony.muted]records[/] {start:,}" + (f"-{end - 1:,}" if count > 1 else "")
        )
        console.print(
            "[cacophony.muted]derived from the schema, not read from a file - so this is what "
            "a run would produce now[/]\n"
        )

        table = Table(box=None, pad_edge=False, header_style="cacophony.muted")
        table.add_column("#", justify="right")
        for name in wanted:
            table.add_column(name)
        for offset, record in enumerate(produced):
            table.add_row(
                f"{start + offset:,}",
                *[_truncate(record.values.get(name)) for name in wanted],
            )
        console.print(table)
