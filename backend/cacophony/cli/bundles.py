"""The ``cacophony bundle`` commands (design document section 72).

    cacophony bundle export project.yaml -o team.cacophony
    cacophony bundle inspect team.cacophony
    cacophony bundle import team.cacophony -d ./team

Kept out of ``main.py`` because a bundle is a trust boundary, and the code that
decides what an archive from somebody else may write deserves to be read on its
own rather than found between two argument lists.

``inspect`` is the interesting one. It reads the manifest, verifies every file
against its recorded hash, and *compiles the bundled project in memory* - so
"will this work" is answerable before anything is written to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..core.errors import CacophonyError
from ..schema.bundle import export_bundle, import_bundle, inspect_bundle
from .theme import console, error_console

__all__ = ["register"]


def register(app: typer.Typer) -> None:
    """Attach the ``bundle`` command group."""
    bundle = typer.Typer(
        name="bundle",
        help="Pack and unpack portable projects (design document section 72).",
        no_args_is_help=True,
    )
    app.add_typer(bundle)

    @bundle.command("export")
    def export(
        project: Path = typer.Argument(..., help="Path to a project YAML or JSON file."),
        out: Path | None = typer.Option(
            None, "--out", "-o", help="Where to write the bundle. Default: beside the project."
        ),
        force: bool = typer.Option(False, "--force", help="Replace an existing bundle."),
    ) -> None:
        """Pack a project and its supporting files into one ``.cacophony`` file.

        Generated datasets are never included - section 72 is explicit, and a
        project is kilobytes while a dataset is gigabytes.
        """
        from .main import _banner

        try:
            path, manifest = export_bundle(project, out, overwrite=force)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        size = path.stat().st_size
        _banner("bundle export", manifest.project)
        console.print(f"[cacophony.ok]wrote[/] {path}  [cacophony.muted]{size:,} bytes[/]")
        console.print(f"  files           {len(manifest.files)}")
        console.print(
            f"  entities        {len(manifest.entities)} "
            f"({sum(manifest.entities.values()):,} records declared)"
        )
        if manifest.recipes_used:
            console.print(f"  recipes         {', '.join(manifest.recipes_used)}")
        for note in manifest.notes:
            console.print(f"  [cacophony.muted]{note}[/]")
        console.print(
            "\n[cacophony.muted]Generated data is deliberately not in here "
            "(design document section 72).[/]"
        )

    @bundle.command("inspect")
    def inspect(
        path: Path = typer.Argument(..., help="Bundle to examine."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Read a bundle without unpacking it, and check that it still works."""
        from .main import _banner

        try:
            report = inspect_bundle(path)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if as_json:
            console.print_json(json.dumps(report.to_dict()))
            raise typer.Exit(code=0 if report.ok else 1)

        manifest = report.manifest
        _banner("bundle", manifest.project or str(path))
        if manifest.description:
            console.print(" ".join(manifest.description.split()))
        console.print(f"  format          v{manifest.format_version}")
        console.print(f"  written by      cacophony {manifest.cacophony_version}")
        console.print(f"  created         {manifest.created_at}")
        console.print(f"  files           {len(report.entries)}  ({report.total_bytes:,} bytes)")
        if manifest.entities:
            listed = ", ".join(
                f"{name} {count:,}" for name, count in sorted(manifest.entities.items())
            )
            console.print(f"  entities        {listed}")
        if manifest.recipes_used:
            console.print(f"  recipes         {', '.join(manifest.recipes_used)}")

        if report.project_ok:
            console.print("  [cacophony.ok]project        compiles[/]")
        else:
            console.print(f"  [cacophony.error]project        {report.project_error}[/]")

        for name in report.tampered:
            error_console.print(
                f"  [cacophony.error]changed[/] {name} does not match its recorded hash"
            )
        for name in report.missing:
            error_console.print(f"  [cacophony.error]missing[/] {name} is in the manifest only")
        for name in report.extra:
            console.print(f"  [cacophony.warn]extra[/] {name} is not in the manifest")

        console.print()
        for name in report.entries:
            if name != "cacophony.json":
                console.print(f"  [cacophony.muted]{name}[/]")

        if not report.ok:
            raise typer.Exit(code=1)

    @bundle.command("import")
    def import_(
        path: Path = typer.Argument(..., help="Bundle to unpack."),
        directory: Path = typer.Option(..., "--directory", "-d", help="Where to unpack it."),
        force: bool = typer.Option(
            False, "--force", help="Write into a directory that is not empty."
        ),
    ) -> None:
        """Unpack a bundle, refusing anything that would write outside the target.

        The whole archive is checked before a byte is written, so a bundle with
        one bad entry leaves nothing behind rather than half a project.
        """
        from .main import _banner

        try:
            target, report = import_bundle(path, directory, overwrite=force)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        _banner("bundle import", report.manifest.project or str(path))
        console.print(f"[cacophony.ok]unpacked[/] {target}")
        console.print(f"  files           {len(report.entries) - 1}")
        if report.tampered:
            error_console.print(
                f"  [cacophony.warn]{len(report.tampered)} file(s) did not match their "
                "recorded hash; the bundle may have been modified[/]"
            )
        if not report.project_ok:
            error_console.print(
                f"  [cacophony.warn]the bundled project does not compile: {report.project_error}[/]"
            )
        console.print(f"\n[cacophony.muted]start with:[/]\n  cacophony plan {target}/project.yaml")
