"""The backend, as a process a window owns (design document section 41).

Everything here exists so that a native shell can start the same server
``cacophony serve`` starts, find out where it landed, and be certain it goes away
again. The interesting parts are the going-away.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

__all__ = [
    "HANDSHAKE_VERSION",
    "Handshake",
    "free_port",
    "make_token",
    "run_sidecar",
    "wait_until_ready",
    "watch_stdin",
]

#: The handshake's own version, so a shell built against an older backend can
#: refuse rather than misread. Printed on the wire, checked by the Rust side.
HANDSHAKE_VERSION = 1

#: What a shell prints to say it is ready. One line, on stdout, then nothing
#: else - so the shell can read a line rather than parse a stream.
HANDSHAKE_PREFIX = "CACOPHONY_HANDSHAKE "


@dataclass(slots=True)
class Handshake:
    """What the shell needs to open a window."""

    url: str
    token: str
    pid: int
    version: int = HANDSHAKE_VERSION

    def to_line(self) -> str:
        return HANDSHAKE_PREFIX + json.dumps(
            {"version": self.version, "url": self.url, "token": self.token, "pid": self.pid}
        )

    @classmethod
    def from_line(cls, line: str) -> Handshake | None:
        """Parse a handshake line, or return None if this is not one.

        Returning None rather than raising is deliberate: the shell reads the
        sidecar's stdout line by line, and most lines are log output.
        """
        text = line.strip()
        if not text.startswith(HANDSHAKE_PREFIX):
            return None
        try:
            payload = json.loads(text[len(HANDSHAKE_PREFIX) :])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or "url" not in payload:
            return None
        return cls(
            url=str(payload["url"]),
            token=str(payload.get("token") or ""),
            pid=int(payload.get("pid") or 0),
            version=int(payload.get("version") or 0),
        )


def free_port(host: str = "127.0.0.1") -> int:
    """A port the operating system says is free, right now.

    Bound and released rather than guessed. There is a window between releasing
    it and the server binding it in which something else could take it, and that
    is unavoidable without passing the socket itself to uvicorn; the window is
    microseconds and the alternative - a fixed port - collides with the
    ``cacophony serve`` somebody already has running, which is not a race but a
    certainty.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def make_token() -> str:
    """A per-launch secret for the window to present.

    A local HTTP server is reachable by every process on the machine. A browser
    tab is an explicit act by a person; a desktop window is not, and one that
    quietly opened an unauthenticated generation API to every other program on a
    shared machine would be a surprise nobody asked for.
    """
    return secrets.token_urlsafe(32)


def wait_until_ready(url: str, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    """Block until the server answers, or the timeout passes.

    Used by tests and by anything that starts the sidecar in-process. The shell
    does not need it: it waits for the handshake line, which is printed after
    the port is bound.
    """
    deadline = time.monotonic() + timeout
    host, _, port = url.removeprefix("http://").removeprefix("https://").partition(":")
    number = int(port or 80)
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, number), timeout=interval):
                return True
        except OSError:
            time.sleep(interval)
    return False


def watch_stdin(on_close: Callable[[], None]) -> threading.Thread:
    """Call ``on_close`` when stdin reaches end of file.

    This is how the server dies with its window, and it is deliberately not a
    signal handler. A shell that crashes, is killed with SIGKILL, or is stopped
    by a supervisor never gets to send a signal - but the operating system
    always closes its pipes. Watching the read end is therefore the one shutdown
    path that cannot be skipped.

    A generator left running after its window closed is the classic desktop
    failure: invisible, still writing files, still occupying a model server.
    """

    def watch() -> None:
        try:
            while True:
                chunk = sys.stdin.readline()
                if not chunk:
                    break
        except (OSError, ValueError):  # pragma: no cover - stdin already gone
            pass
        on_close()

    thread = threading.Thread(target=watch, name="cacophony-desktop-stdin", daemon=True)
    thread.start()
    return thread


def run_sidecar(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    token: str | None = None,
    store_path: str | Path | None = None,
    studio: str | Path | None = None,
    handshake: bool = True,
    watch_parent: bool = True,
    log_level: str = "warning",
    on_ready: Callable[[Handshake], None] | None = None,
) -> None:
    """Serve the Studio for a desktop shell, and stop when the shell does.

    The same application ``cacophony serve`` builds - section 41 requires that
    web deployment remain possible, and the cheapest way to guarantee it is to
    have no second application to keep in step.
    """
    import uvicorn

    from ..api.app import create_app

    chosen_port = port or free_port(host)
    chosen_token = token if token is not None else make_token()

    app = create_app(store_path=store_path, static_dir=studio, token=chosen_token or None)
    config = uvicorn.Config(
        app,
        host=host,
        port=chosen_port,
        log_level=log_level,
        # The handshake owns stdout. Uvicorn's banner on the same stream would
        # be read by the shell as a line it could not parse - harmless, but the
        # handshake is the one thing that must be unambiguous.
        access_log=False,
    )
    server = uvicorn.Server(config)

    greeting = Handshake(url=f"http://{host}:{chosen_port}", token=chosen_token, pid=os.getpid())

    if watch_parent:
        watch_stdin(lambda: setattr(server, "should_exit", True))

    def announce() -> None:
        # Printed once the port is bound, so a shell that opens a window the
        # instant it reads this never races a server that is not listening.
        if wait_until_ready(greeting.url, timeout=30.0):
            if handshake:
                print(greeting.to_line(), flush=True)
            if on_ready is not None:
                on_ready(greeting)

    threading.Thread(target=announce, name="cacophony-desktop-ready", daemon=True).start()
    server.run()
