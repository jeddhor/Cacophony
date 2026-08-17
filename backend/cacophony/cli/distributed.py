"""The distributed commands (design document sections 84, 95).

    cacophony cluster project.yaml -o out/ --workers 8
    cacophony controller project.yaml --port 8787
    cacophony worker project.yaml --controller http://box:8787 -o /mnt/shared

``cluster`` is a whole distributed run in one process; the other two are the
same run with a network in the middle. They share every line below the
transport, which is deliberate: the local form is what makes the distributed
form trustworthy, because a lease protocol that only ever runs across machines
is a lease protocol nobody tests.

``generate`` is untouched and remains the single-node path - it is the one with
run records, checkpoints and resume. These commands trade that bookkeeping for
parallelism, and the trade is honest because a shard needs no checkpoint: if it
does not finish, it is simply done again.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ..core.errors import CacophonyError
from ..distributed import Capabilities, Controller, HttpTransport, Worker, assemble
from ..distributed.assembly import CONCATENABLE
from ..distributed.cluster import run_cluster
from ..distributed.worker import RELATIONAL_FORMATS
from ..outputs import OUTPUT_FORMATS
from .theme import console, error_console

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.table import Table

__all__ = ["register"]

#: Where a worker reads its controller token from, so it is never a flag that
#: lands in a shell history or a process listing (section 63).
TOKEN_ENV = "CACOPHONY_CONTROLLER_TOKEN"


def _status_table(status: dict[str, Any]) -> Table:
    """Draw what the controller is doing."""
    from rich.table import Table

    stats = status.get("stats") or {}
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cacophony.muted", justify="right")
    table.add_column()

    progress = status.get("progress", 0.0)
    records = stats.get("records", 0)
    total = status.get("total_records", 0)
    table.add_row("progress", f"{progress * 100:5.1f}%  {records:,} / {total:,}")
    table.add_row("rate", f"{stats.get('records_per_second', 0):,.0f}/s")

    states = status.get("states") or {}
    table.add_row(
        "shards",
        "  ".join(f"{name} {count}" for name, count in sorted(states.items())) or "-",
    )
    if stats.get("shards_reassigned"):
        table.add_row("reassigned", f"{stats['shards_reassigned']} (a worker went quiet)")
    if status.get("unmet_capabilities"):
        table.add_row(
            "waiting for", f"[cacophony.warn]{', '.join(status['unmet_capabilities'])}[/]"
        )

    workers = Table(box=None, pad_edge=False, header_style="cacophony.muted")
    workers.add_column("worker")
    workers.add_column("capabilities")
    workers.add_column("shards", justify="right")
    workers.add_column("records", justify="right")
    workers.add_column("rate", justify="right")
    workers.add_column("state")
    for worker in status.get("workers") or []:
        alive = worker.get("alive")
        workers.add_row(
            str(worker.get("id")),
            ", ".join(worker.get("capabilities") or []),
            f"{worker.get('shards_completed', 0):,}",
            f"{worker.get('records_produced', 0):,}",
            f"{worker.get('records_per_second', 0):,.0f}/s",
            "[cacophony.ok]working[/]" if alive else "[cacophony.error]silent[/]",
        )
    table.add_row("workers", workers)
    return table


def register(app: typer.Typer) -> None:
    """Attach ``cluster``, ``controller`` and ``worker``."""

    # -- one machine, many workers ------------------------------------------- #

    @app.command()
    def cluster(
        project: Path = typer.Argument(..., help="Path to a project YAML or JSON file."),
        output: Path = typer.Option(
            Path("output"), "--output", "-o", help="Directory for the shard files."
        ),
        workers: int = typer.Option(4, "--workers", "-w", help="Workers to run in this process."),
        fmt: str = typer.Option("jsonl", "--format", "-f", help="Output format for each shard."),
        shard_size: int = typer.Option(
            50_000, "--shard-size", help="Records per shard. Smaller shards balance better."
        ),
        batch_size: int = typer.Option(1_000, "--batch-size", help="Records per write."),
        records: int | None = typer.Option(
            None, "--records", "-n", help="Override every entity's record count."
        ),
        join: bool = typer.Option(
            True, "--join/--no-join", help="Concatenate the parts when the run finishes."
        ),
        assets_dir: Path | None = typer.Option(
            None, "--assets-dir", help="Shared directory for generated media."
        ),
        seed: int | None = typer.Option(None, "--seed", help="Override the project seed."),
        quiet: bool = typer.Option(False, "--quiet", "-q", help="No live display."),
    ) -> None:
        """Generate a project across several workers on this machine (section 95).

        The parts are named after their offset, so the joined file is
        byte-identical to what 'cacophony generate' would have written.
        """
        from rich.live import Live

        from .main import _banner, _load

        compiled = _load(project, seed)
        _check_format(fmt, join)
        counts = dict.fromkeys(compiled.entity_order, records) if records else None
        assets = _asset_store(assets_dir or (output / "assets"), compiled)

        _banner("cluster", f"{compiled.name} -> {output}")
        console.print(f"[cacophony.muted]workers[/] {workers}  shard size {shard_size:,}\n")

        if quiet:
            result = asyncio.run(
                run_cluster(
                    compiled,
                    output_dir=output,
                    workers=workers,
                    output_format=fmt,
                    shard_size=shard_size,
                    batch_size=batch_size,
                    counts=counts,
                    assets=assets,
                )
            )
        else:
            with Live(console=console, refresh_per_second=6, transient=False) as live:

                def show(status: dict[str, Any]) -> None:
                    live.update(_status_table(status))

                result = asyncio.run(
                    run_cluster(
                        compiled,
                        output_dir=output,
                        workers=workers,
                        output_format=fmt,
                        shard_size=shard_size,
                        batch_size=batch_size,
                        counts=counts,
                        assets=assets,
                        on_progress=show,
                    )
                )

        console.print()
        console.print(
            f"[cacophony.ok]done[/] {result.records:,} records in {result.seconds:,.1f}s "
            f"({result.rate:,.0f}/s) across {result.shards} shards"
        )

        if result.failures:
            error_console.print(
                f"[cacophony.error]failed[/] {len(result.failures)} shards did not complete:"
            )
            for lease in result.failures[:5]:
                error_console.print(f"  {lease['entity']}[{lease['offset']}] {lease['error']}")

        if join:
            console.print("\n[cacophony.accent]joined[/]")
            for name in compiled.entity_order:
                try:
                    joined = assemble(output, name, fmt, remove_parts=True)
                except CacophonyError as exc:
                    error_console.print(f"  [cacophony.error]{name}[/] {exc}")
                    continue
                console.print(
                    f"  {joined.path}  [cacophony.muted]{joined.records:,} records "
                    f"from {joined.parts} parts[/]"
                )
        console.print()

        if result.failures:
            raise typer.Exit(code=1)

    # -- the controller ------------------------------------------------------- #

    @app.command()
    def controller(
        project: Path = typer.Argument(..., help="Path to a project YAML or JSON file."),
        host: str = typer.Option("0.0.0.0", "--host", help="Interface to bind."),
        port: int = typer.Option(8787, "--port", help="Port to listen on."),
        shard_size: int = typer.Option(50_000, "--shard-size", help="Records per shard."),
        lease_seconds: float = typer.Option(
            30.0, "--lease-seconds", help="How long a shard is granted for before it is reclaimed."
        ),
        max_attempts: int = typer.Option(
            3, "--max-attempts", help="Give up on a shard after this many failed attempts."
        ),
        records: int | None = typer.Option(
            None, "--records", "-n", help="Override every entity's record count."
        ),
        seed: int | None = typer.Option(None, "--seed", help="Override the project seed."),
        log_level: str = typer.Option("info", "--log-level"),
    ) -> None:
        """Serve shards to worker nodes (sections 84, 95).

            cacophony controller project.yaml --port 8787

        Set CACOPHONY_CONTROLLER_TOKEN to require the same token from workers.
        """
        try:
            import uvicorn

            from ..distributed.service import create_controller_app
        except ImportError as exc:
            error_console.print(
                "[cacophony.error]error[/] the controller needs FastAPI and uvicorn. "
                "Install them with: pip install 'cacophony[api]'"
            )
            raise typer.Exit(code=2) from exc

        from ..observability.logging import configure_logging
        from .main import _banner, _load

        compiled = _load(project, seed)
        counts = dict.fromkeys(compiled.entity_order, records) if records else None
        scheduler = Controller(
            compiled,
            shard_size=shard_size,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            counts=counts,
        )
        configure_logging(log_level)

        token = os.environ.get(TOKEN_ENV) or None
        service = create_controller_app(scheduler, token=token)

        _banner("controller", f"{compiled.name} on http://{host}:{port}")
        console.print(
            f"[cacophony.muted]shards[/] {len(scheduler.leases):,} of up to {shard_size:,} "
            f"records ({scheduler.total_records:,} total)"
        )
        console.print(f"[cacophony.muted]schema[/] {scheduler.schema_hash[:16]}")
        console.print(
            f"[cacophony.muted]token [/] {'required' if token else f'none - set {TOKEN_ENV}'}"
        )
        console.print(
            f"\n[cacophony.muted]workers join with:[/]\n"
            f"  cacophony worker {project} --controller http://<this-host>:{port} -o /mnt/shared\n"
        )

        uvicorn.run(service, host=host, port=port, log_level=log_level)

    # -- a worker ------------------------------------------------------------- #

    @app.command()
    def worker(
        project: Path = typer.Argument(..., help="The same project the controller is running."),
        controller_url: str = typer.Option(
            ..., "--controller", "-c", help="Controller base URL, e.g. http://box:8787."
        ),
        output: Path = typer.Option(
            Path("output"), "--output", "-o", help="Where to write shards. Share it if you can."
        ),
        fmt: str = typer.Option("jsonl", "--format", "-f", help="Output format for each shard."),
        name: str | None = typer.Option(None, "--name", help="Worker id. Default: a random one."),
        capabilities: str | None = typer.Option(
            None,
            "--capabilities",
            help="Comma-separated override, e.g. deterministic,image. Default: detected.",
        ),
        concurrency: int = typer.Option(
            1, "--concurrency", help="Shards to lease at once. Raise it on a fast node."
        ),
        batch_size: int = typer.Option(1_000, "--batch-size", help="Records per write."),
        records: int | None = typer.Option(
            None, "--records", "-n", help="Must match the controller's --records."
        ),
        assets_dir: Path | None = typer.Option(
            None, "--assets-dir", help="Shared directory for generated media."
        ),
        idle_timeout: float = typer.Option(
            60.0, "--idle-timeout", help="Leave after this long with no work."
        ),
        seed: int | None = typer.Option(None, "--seed", help="Must match the controller's --seed."),
    ) -> None:
        """Join a controller and generate shards (sections 84, 95).

            cacophony worker project.yaml -c http://box:8787 -o /mnt/shared

        The project must be the one the controller is running; a worker whose
        schema hashes differently is refused rather than allowed to contribute
        records from a different world.
        """
        from .main import _banner, _load

        compiled = _load(project, seed)
        _check_format(fmt, join=False)
        counts = dict.fromkeys(compiled.entity_order, records) if records else None

        try:
            declared = Capabilities.of(capabilities.split(",")) if capabilities else None
        except ValueError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        transport = HttpTransport(
            controller_url, token=os.environ.get(TOKEN_ENV) or None, timeout=60.0
        )
        node = Worker(
            compiled,
            transport,
            output_dir=output,
            output_format=fmt,
            capabilities=declared,
            worker_id=name,
            concurrency=concurrency,
            batch_size=batch_size,
            counts=counts,
            idle_timeout=idle_timeout,
        )
        # After construction, because the manifest is named after the worker
        # and the worker names itself when it was not given a name.
        node.assets = _asset_store(assets_dir or (output / "assets"), compiled, node=node.id)

        _banner("worker", f"{node.id} -> {controller_url}")
        console.print(f"[cacophony.muted]can do[/] {node.capabilities.render()}")
        console.print(f"[cacophony.muted]writes[/] {output}\n")

        try:
            stats = asyncio.run(_work(node))
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=1) from exc
        except KeyboardInterrupt:
            console.print("\n[cacophony.muted]stopped[/]")
            raise typer.Exit(code=130) from None

        console.print(
            f"[cacophony.ok]done[/] {stats.records:,} records in {stats.shards} shards "
            f"({stats.rate:,.0f}/s)"
        )
        if stats.abandoned:
            console.print(
                f"[cacophony.muted]{stats.abandoned} shards were reassigned while in flight "
                "and discarded[/]"
            )
        if stats.failures:
            error_console.print(f"[cacophony.error]{stats.failures} shards failed[/]")
            for result in node.results:
                if result.error:
                    error_console.print(f"  {result.shard} {result.error}")
            raise typer.Exit(code=1)


async def _work(node: Worker) -> Any:
    try:
        return await node.run()
    finally:
        await node.transport.close()


def _check_format(fmt: str, join: bool) -> None:
    if fmt.lower() not in OUTPUT_FORMATS:
        known = ", ".join(sorted(OUTPUT_FORMATS))
        error_console.print(f"[cacophony.error]error[/] unknown format '{fmt}'. Available: {known}")
        raise typer.Exit(code=2)
    if fmt.lower() in RELATIONAL_FORMATS:
        error_console.print(
            f"[cacophony.error]error[/] '{fmt}' cannot be produced by a distributed run - each "
            "shard would be a separate database and the foreign keys would not resolve. "
            "Generate 'jsonl' or 'parquet' parts and load them, or run 'cacophony generate'."
        )
        raise typer.Exit(code=2)
    if join and fmt.lower() not in CONCATENABLE:
        error_console.print(
            f"[cacophony.error]error[/] '{fmt}' parts cannot be joined - the format has a "
            "per-file footer. Pass --no-join and read the directory of parts."
        )
        raise typer.Exit(code=2)


def _asset_store(directory: Path, compiled: Any, *, node: str | None = None) -> Any:
    """An asset store, but only for a project that generates media.

    Shared artifact storage (section 95) is a mounted directory, not a service:
    assets are addressed by content hash (section 81), so two nodes writing the
    same file write the same bytes to the same name, and a third reading it
    cannot tell which one won.

    The manifest is the one part that is not content-addressed, so each node
    appends to its own. Two machines appending lines to one file across a
    network filesystem is the one way this could have corrupted anything.
    """
    from ..assets.store import AssetStore

    produces_media = any(
        type(field.generator).requires_provider in ("image", "speech")
        for entity in compiled.entities.values()
        for field in entity.fields
    )
    if not produces_media:
        return None
    return AssetStore(directory, manifest_name=f"manifest.{node}.jsonl" if node else None)
