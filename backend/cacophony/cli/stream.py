"""The ``cacophony stream`` command (design document sections 35, 94).

    Produce approximately 250 authentication events/sec, 50 endpoint
    events/sec, 8 alerts/minute.

Kept out of ``main.py`` because the live display is real behaviour rather than
argument parsing: a stream runs for hours, and what it shows while it does is
the only way anyone knows it is still doing what they asked.

The display answers three questions, in the order an operator asks them:

    is it keeping up?      achieved rate against the rate requested
    where is it going?     per destination, with failures
    what is it making?     per entity, with running totals

Attainment is the number that matters and the one a naive dashboard omits. A
stream that reports "12,000 records delivered" while quietly running at sixty
per cent of the rate it was given is measuring the wrong thing.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.live import Live
from rich.table import Table

from ..core.errors import CacophonyError
from ..live import LiveStream, StreamConfig, create_sink, parse_rate
from .theme import console, error_console

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledProject

__all__ = ["parse_rates", "run_stream"]


def parse_rates(values: list[str], compiled: CompiledProject) -> dict[str, Any]:
    """Read ``--rate authentication=250/s`` arguments.

    A bare rate with no entity applies to the project's last entity, which is
    almost always the event table - the one people actually want to stream.
    """
    rates: dict[str, Any] = {}
    for value in values:
        entity, separator, rate = value.partition("=")
        if not separator:
            if not compiled.entity_order:
                raise CacophonyError("this project defines no entities")
            entity, rate = compiled.entity_order[-1], value

        entity = entity.strip()
        if entity not in compiled.entities:
            known = ", ".join(compiled.entity_order)
            raise CacophonyError(f"'{entity}' is not an entity. Known: {known}")
        rates[entity] = parse_rate(rate.strip())
    return rates


def build_sinks(destinations: list[str], options: dict[str, Any]) -> list[Any]:
    """Build every destination, or explain which one could not be built."""
    sinks = []
    for destination in destinations or ["stdout"]:
        try:
            sink = create_sink(destination)
        except CacophonyError as exc:
            raise CacophonyError(f"destination '{destination}': {exc}") from exc
        for key, value in options.items():
            if value is not None and hasattr(sink, key):
                setattr(sink, key, value)
        sinks.append(sink)
    return sinks


def run_stream(stream: LiveStream, *, quiet: bool = False, piping: bool = False) -> int:
    """Drive a stream, showing what it is doing, until it stops.

    Ctrl-C stops it the way a long-running process should be stopped: the
    current batch finishes, destinations are closed, and the totals are
    reported. A stream that loses its last thousand records to an impatient
    signal handler is a stream nobody trusts.
    """

    # When stdout carries the data, every human-readable byte must go to
    # stderr - otherwise `cacophony stream ... | jq` chokes on the dashboard.
    out = error_console if piping else console

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        stopping = False

        def interrupt() -> None:
            nonlocal stopping
            if stopping:
                return
            stopping = True
            out.print("\n[cacophony.warn]stopping[/] finishing the current batch…")
            stream.stop()

        for name in ("SIGINT", "SIGTERM"):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(getattr(signal, name), interrupt)

        task = asyncio.create_task(stream.run())
        if quiet:
            await task
            return

        with Live(_dashboard(stream), console=out, refresh_per_second=4) as live:
            while not task.done():
                live.update(_dashboard(stream))
                await asyncio.sleep(0.25)
            live.update(_dashboard(stream))
        await task

    try:
        asyncio.run(drive())
    except KeyboardInterrupt:  # pragma: no cover - a second Ctrl-C
        stream.stop()
    except CacophonyError as exc:
        error_console.print(f"[cacophony.error]error[/] {exc}")
        return 1

    _summarise(stream, out)
    return 0 if stream.state in ("completed", "cancelled") else 1


def _dashboard(stream: LiveStream) -> Table:
    """Section 94's streaming dashboard, in a terminal."""
    stats = stream.stats
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")

    attainment = stats.attainment
    style = "cacophony.ok" if attainment >= 0.95 else "cacophony.warn"
    table.add_row(
        f"[cacophony.brand]CACOPHONY[/] [cacophony.muted]streaming[/] {stream.state}",
        f"[{style}]{stats.current_rate():,.0f}/s of {stats.target_rate:,.0f}/s"
        f"  ({attainment:.0%})[/]",
    )
    table.add_row(
        f"[cacophony.muted]{stats.generated:,} generated · "
        f"{stats.delivered:,} delivered · {stats.elapsed:,.0f}s[/]",
        f"[cacophony.error]{stats.dropped:,} dropped[/]" if stats.dropped else "",
    )
    table.add_row("", "")

    for entity in stream.streams.values():
        produced = stats.by_entity.get(entity.entity, 0)
        table.add_row(
            f"  [cacophony.accent]{entity.entity}[/] [cacophony.muted]{entity.rate.render()}[/]",
            f"{produced:,}",
        )

    for sink in stream.config.sinks:
        detail = sink.describe()
        problem = f"  [cacophony.error]{detail['failed']:,} failed[/]" if detail["failed"] else ""
        table.add_row(
            f"  [cacophony.muted]→ {sink.name}[/]{problem}",
            f"[cacophony.muted]{detail['delivered']:,}[/]",
        )
    return table


def _summarise(stream: LiveStream, out: Any = console) -> None:
    stats = stream.stats
    out.rule(style="cacophony.rule")
    out.print(
        f"[cacophony.ok]{stream.state}[/]  {stats.generated:,} records in {stats.elapsed:,.1f}s"
    )
    out.print(f"  rate            {stats.mean_rate:,.1f}/s of {stats.target_rate:,.1f}/s requested")
    if stats.attainment < 0.95:
        out.print(
            "  [cacophony.warn]attainment      "
            f"{stats.attainment:.1%} - generation or a destination could not keep up[/]"
        )
    for sink in stream.config.sinks:
        detail = sink.describe()
        line = f"  {sink.name:<15} {detail['delivered']:,} delivered"
        if detail["failed"]:
            line += f", [cacophony.error]{detail['failed']:,} failed[/]"
        out.print(line)
        if detail.get("last_error"):
            out.print(f"    [cacophony.muted]{detail['last_error']}[/]")
    if stream.error:
        error_console.print(f"[cacophony.error]error[/] {stream.error}")
    out.print(
        f"\n[cacophony.muted]resume where this left off:[/] --from "
        f"{max((s.index for s in stream.streams.values()), default=0)}"
    )


def register(app: typer.Typer) -> None:
    """Attach the ``stream`` command."""

    @app.command()
    def stream(
        project: Path = typer.Argument(..., help="Path to a project YAML or JSON file."),
        rate: list[str] = typer.Option(
            [],
            "--rate",
            "-r",
            help="ENTITY=RATE, e.g. authentication=250/s. Repeatable.",
        ),
        to: list[str] = typer.Option(
            [],
            "--to",
            "-t",
            help="Destination: stdout, file://path, syslog://host:514, https://url, kafka://broker/topic.",
        ),
        seconds: float | None = typer.Option(
            None, "--seconds", "-s", help="Stop after this long. Default: until interrupted."
        ),
        records: int | None = typer.Option(
            None, "--records", "-n", help="Stop after this many records."
        ),
        batch_size: int = typer.Option(100, "--batch-size", help="Records per delivery."),
        flush: float = typer.Option(
            1.0, "--flush", help="Deliver a partial batch after this many seconds."
        ),
        start: int = typer.Option(
            0, "--from", help="Start indices here, to continue a previous stream."
        ),
        follow_shape: bool = typer.Option(
            False,
            "--follow-shape",
            help="Modulate the rate by the project timeline's shape: quiet at night.",
        ),
        historical: bool = typer.Option(
            False,
            "--historical",
            help="Keep generated timestamps instead of stamping events with the wall clock.",
        ),
        scenario_cycle: float = typer.Option(
            3600.0,
            "--scenario-cycle",
            help="Seconds a scenario window repeats over. A stream has no end, so incidents recur.",
        ),
        on_error: str = typer.Option(
            "continue", "--on-error", help="continue or abort when a destination rejects records."
        ),
        seed: int | None = typer.Option(None, "--seed", help="Override the project seed."),
        quiet: bool = typer.Option(
            False, "--quiet", "-q", help="No dashboard; useful when piping."
        ),
    ) -> None:
        """Generate continuously and deliver it somewhere (section 35).

        Turns Cacophony into a workload generator: a rate per entity, one or
        more destinations, and a stream that runs until you stop it.

            cacophony stream project.yaml -r authentication=250/s -t syslog://siem:514
        """
        from .main import _load

        compiled = _load(project, seed)
        try:
            rates = parse_rates(rate, compiled)
            if not rates:
                raise CacophonyError(
                    "a stream needs at least one rate, e.g. "
                    f"--rate {compiled.entity_order[-1]}=100/s"
                )
            sinks = build_sinks(to, {})
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        config = StreamConfig(
            rates=rates,
            sinks=sinks,
            batch_size=max(1, batch_size),
            flush_seconds=max(0.01, flush),
            duration_seconds=seconds,
            max_records=records,
            start_index=max(0, start),
            live_time=not historical,
            follow_shape=follow_shape,
            on_error=on_error,
            scenario_cycle_seconds=max(1.0, scenario_cycle),
        )

        try:
            live = LiveStream(compiled, config)
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        # A dashboard drawn over piped output would corrupt it.
        piping = any(sink.name == "stdout" for sink in sinks)
        code = run_stream(live, quiet=quiet or piping, piping=piping)
        if code:
            raise typer.Exit(code=code)
