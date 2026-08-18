"""Cacophony as a desktop application (design document section 41).

    Tauri is preferable if practical because the application primarily needs to
    host the web UI while the Python backend performs generation. However, web
    deployment should remain possible.

That last sentence is the property most of these tests defend: there is no
desktop *mode* in the backend. `cacophony desktop` serves the same application
`cacophony serve` serves, and the served behaviour is unchanged — asserted
directly, because a second application to keep in step is exactly how "web
deployment should remain possible" stops being true.

The rest is about the two ways a desktop application goes wrong that a served one
does not.

**A local server is reachable by every process on the machine.** A browser tab is
an explicit act by a person; a window is not. So the desktop backend requires a
per-launch token — over HTTP *and* over WebSockets, which is the case that was
initially missed because Starlette's HTTP middleware never sees a socket
handshake.

**The backend must die with its window**, including when the window is killed
outright and never gets to run an exit handler. The one shutdown path that
survives that is the operating system closing the pipe, so the backend watches
its own stdin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cacophony.desktop import (
    HANDSHAKE_VERSION,
    Handshake,
    free_port,
    make_token,
    wait_until_ready,
)

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / ".venv" / "bin" / "cacophony"


# --------------------------------------------------------------------------- #
# The handshake
# --------------------------------------------------------------------------- #


class TestHandshake:
    def test_it_round_trips(self) -> None:
        greeting = Handshake(url="http://127.0.0.1:1234", token="abc", pid=99)
        parsed = Handshake.from_line(greeting.to_line())
        assert parsed is not None
        assert (parsed.url, parsed.token, parsed.pid) == ("http://127.0.0.1:1234", "abc", 99)
        assert parsed.version == HANDSHAKE_VERSION

    def test_it_is_one_line(self) -> None:
        """The shell reads a line, not a stream."""
        assert "\n" not in Handshake(url="http://x", token="t", pid=1).to_line()

    @pytest.mark.parametrize(
        "line",
        [
            "INFO:     Uvicorn running on http://127.0.0.1:8000",
            "",
            "CACOPHONY_HANDSHAKE not json",
            "CACOPHONY_HANDSHAKE {}",
            'CACOPHONY_HANDSHAKE ["a"]',
        ],
    )
    def test_anything_else_is_not_a_handshake(self, line: str) -> None:
        """The shell reads every line the backend prints; most are logs."""
        assert Handshake.from_line(line) is None

    def test_a_version_travels_so_a_shell_can_refuse(self) -> None:
        payload = json.loads(Handshake(url="http://x", token="t", pid=1).to_line().split(" ", 1)[1])
        assert payload["version"] == HANDSHAKE_VERSION


class TestPortAndToken:
    def test_a_free_port_is_actually_free(self) -> None:
        import socket

        port = free_port()
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    def test_ports_differ_between_launches(self) -> None:
        """A fixed port collides with the `cacophony serve` already running."""
        assert len({free_port() for _ in range(5)}) > 1

    def test_a_token_is_long_and_unguessable(self) -> None:
        first, second = make_token(), make_token()
        assert first != second
        assert len(first) >= 32

    def test_waiting_gives_up_rather_than_hanging(self) -> None:
        assert wait_until_ready(f"http://127.0.0.1:{free_port()}", timeout=0.3) is False


# --------------------------------------------------------------------------- #
# The token gate
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store.db"


def app_with_token(store: Path, token: str | None, studio: Path | None = None):
    pytest.importorskip("fastapi")
    from cacophony.api.app import create_app

    return create_app(store_path=store, token=token, static_dir=studio)


class TestTokenGate:
    def test_a_served_cacophony_is_unchanged(self, store: Path) -> None:
        """Section 41: web deployment must remain possible.

        `cacophony serve` passes no token, and nothing about its behaviour may
        change because the desktop shell exists.
        """
        from fastapi.testclient import TestClient

        with TestClient(app_with_token(store, None)) as client:
            assert client.get("/api/system").status_code == 200
            with client.websocket_connect("/api/runs/nope/stream") as socket:
                assert socket.receive_json()["kind"] == "error"

    def test_http_without_a_token_is_refused(self, store: Path) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app_with_token(store, "sekrit")) as client:
            response = client.get("/api/system")
            assert response.status_code == 401
            assert response.json()["error"] == "unauthorised"

    def test_http_with_the_token_is_allowed(self, store: Path) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app_with_token(store, "sekrit")) as client:
            assert (
                client.get("/api/system", headers={"authorization": "Bearer sekrit"}).status_code
                == 200
            )
            assert client.get("/api/system?token=sekrit").status_code == 200

    def test_a_wrong_token_is_refused(self, store: Path) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app_with_token(store, "sekrit")) as client:
            assert (
                client.get("/api/system", headers={"authorization": "Bearer nope"}).status_code
                == 401
            )
            assert client.get("/api/system?token=nope").status_code == 401

    def test_websockets_are_guarded_too(self, store: Path) -> None:
        """The case that was missed first time round.

        `@app.middleware("http")` only sees `scope["type"] == "http"`, so a
        WebSocket handshake passed straight through it — leaving the two socket
        routes open while every other route was guarded. A gap in a security
        control is worse than no control, because the control is what stops
        anyone looking. The gate is pure ASGI for this reason.
        """
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app_with_token(store, "sekrit")) as client:
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect("/api/runs/nope/stream"),
            ):
                pass

            with client.websocket_connect("/api/runs/nope/stream?token=sekrit") as socket:
                assert socket.receive_json()["kind"] == "error"

    def test_the_studio_itself_is_not_gated(self, store: Path, tmp_path: Path) -> None:
        """It is static files carrying no data, and the window has to load.

        A Studio of its own rather than the built one: `npm run build` output is
        not in the repository, so a test that asked for `/` and hoped would pass
        here and fail in a fresh clone — which is exactly how it was caught.
        """
        from fastapi.testclient import TestClient

        studio = tmp_path / "studio"
        studio.mkdir()
        (studio / "index.html").write_text("<!doctype html><title>Cacophony</title>")

        with TestClient(app_with_token(store, "sekrit", studio)) as client:
            response = client.get("/")
            assert response.status_code == 200
            assert "Cacophony" in response.text


# --------------------------------------------------------------------------- #
# The sidecar, as a real process
# --------------------------------------------------------------------------- #


def read_handshake(process: subprocess.Popen[str], *, timeout: float = 60.0) -> Handshake:
    """Read lines until the handshake appears, as the shell does."""
    deadline = time.monotonic() + timeout
    assert process.stdout is not None
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        found = Handshake.from_line(line)
        if found is not None:
            return found
    raise AssertionError("the backend never announced itself")


@pytest.mark.skipif(not CLI.exists(), reason="the CLI is not installed in this checkout")
class TestSidecarProcess:
    """The sidecar as the shell actually runs it: a subprocess with a pipe."""

    def _spawn(self, tmp_path: Path, *extra: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [str(CLI), "desktop", "--store", str(tmp_path / "store.db"), *extra],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def test_it_announces_a_url_and_a_token(self, tmp_path: Path) -> None:
        process = self._spawn(tmp_path)
        try:
            greeting = read_handshake(process)
            assert greeting.url.startswith("http://127.0.0.1:")
            assert len(greeting.token) >= 32
            assert greeting.pid == process.pid
        finally:
            process.stdin.close()  # type: ignore[union-attr]
            process.wait(timeout=30)

    def test_the_server_is_listening_by_the_time_it_announces(self, tmp_path: Path) -> None:
        """A shell that opened a window on the handshake must not race."""
        process = self._spawn(tmp_path)
        try:
            greeting = read_handshake(process)
            assert wait_until_ready(greeting.url, timeout=0.5)
        finally:
            process.stdin.close()  # type: ignore[union-attr]
            process.wait(timeout=30)

    def test_the_api_needs_the_token(self, tmp_path: Path) -> None:
        import urllib.error
        import urllib.request

        process = self._spawn(tmp_path)
        try:
            greeting = read_handshake(process)

            request = urllib.request.Request(
                f"{greeting.url}/api/system",
                headers={"Authorization": f"Bearer {greeting.token}"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                assert json.loads(response.read())["version"]

            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{greeting.url}/api/system", timeout=10)
            assert caught.value.code == 401
        finally:
            process.stdin.close()  # type: ignore[union-attr]
            process.wait(timeout=30)

    def test_it_stops_when_stdin_closes(self, tmp_path: Path) -> None:
        """How the backend dies with its window."""
        process = self._spawn(tmp_path)
        read_handshake(process)
        process.stdin.close()  # type: ignore[union-attr]
        assert process.wait(timeout=30) == 0

    @pytest.mark.skipif(os.name != "posix", reason="SIGKILL is POSIX")
    def test_it_stops_when_the_shell_is_killed_outright(self, tmp_path: Path) -> None:
        """The claim the design rests on, tested the hard way.

        A shell that is SIGKILLed never runs an exit handler, never drops its
        managed state, and never sends a signal. The operating system closes its
        pipes anyway — which is why the backend watches stdin rather than
        trusting a signal.

        Simulated with an intermediate process standing in for the shell: it
        holds the pipe, and is killed without warning.
        """
        shell = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,sys,time;"
                    f"p=subprocess.Popen([{str(CLI)!r},'desktop','--store',"
                    f"{str(tmp_path / 'kill.db')!r}],stdin=subprocess.PIPE,"
                    "stdout=subprocess.PIPE,text=True,bufsize=1);"
                    # Stripped: the handshake already ends in a newline, and
                    # `print` adding another puts a blank line between it and
                    # the pid the test reads next.
                    "print(p.stdout.readline().strip(),flush=True);"
                    "print(p.pid,flush=True);"
                    "time.sleep(300)"
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            assert shell.stdout is not None
            greeting = Handshake.from_line(shell.stdout.readline())
            assert greeting is not None
            backend_pid = int(shell.stdout.readline().strip())
            assert wait_until_ready(greeting.url, timeout=10)

            shell.kill()  # No handler, no Drop, nothing but the closed pipe.
            shell.wait(timeout=15)

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    os.kill(backend_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.2)
            else:
                os.kill(backend_pid, 9)
                raise AssertionError("the backend outlived the shell that was killed")
        finally:
            if shell.poll() is None:  # pragma: no cover - only on a failed run
                shell.kill()

    def test_two_at_once_do_not_collide(self, tmp_path: Path) -> None:
        """A desktop app that refused to start because a terminal was busy
        would be a bad desktop app."""
        first = self._spawn(tmp_path / "a")
        second = self._spawn(tmp_path / "b")
        try:
            (tmp_path / "a").mkdir(exist_ok=True)
            (tmp_path / "b").mkdir(exist_ok=True)
            one = read_handshake(first)
            two = read_handshake(second)
            assert one.url != two.url
            assert one.token != two.token
        finally:
            for process in (first, second):
                process.stdin.close()  # type: ignore[union-attr]
                process.wait(timeout=30)


# --------------------------------------------------------------------------- #
# The shell
# --------------------------------------------------------------------------- #


DESKTOP = REPO / "desktop" / "src-tauri"


class TestItSaysWhatItIs:
    """`cacophony desktop` is the backend, and people type it expecting a window.

    The command's name reads like "open the desktop app", so run by hand it says
    what it is and how to get a window. Run by the shell it must say nothing at
    all: stdout carries the handshake, and a surprise line on it is a line the
    shell has to guess about.
    """

    def test_a_pipe_gets_the_handshake_and_nothing_else(self, tmp_path: Path) -> None:
        process = subprocess.Popen(
            [str(CLI), "desktop", "--store", str(tmp_path / "store.db")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            greeting = read_handshake(process)
            assert greeting.url
        finally:
            process.stdin.close()  # type: ignore[union-attr]
            remaining_out, errors = process.communicate(timeout=30)
        assert remaining_out.strip() == ""
        assert "desktop window" not in errors

    def test_a_terminal_gets_told_where_the_window_is(self, tmp_path: Path) -> None:
        """A pseudo-terminal, because the hint is gated on isatty."""
        import pty

        primary, secondary = pty.openpty()
        process = subprocess.Popen(
            [str(CLI), "desktop", "--store", str(tmp_path / "store.db"), "--keep-running"],
            stdin=subprocess.DEVNULL,
            stdout=secondary,
            stderr=secondary,
            text=True,
        )
        os.close(secondary)
        try:
            deadline = time.monotonic() + 60.0
            seen = ""
            while time.monotonic() < deadline and "Ctrl-C" not in seen:
                try:
                    chunk = os.read(primary, 4096)
                except OSError:  # pragma: no cover - the far end closed
                    break
                if not chunk:
                    break
                seen += chunk.decode(errors="replace")
        finally:
            process.terminate()
            process.wait(timeout=30)
            os.close(primary)

        # Colour codes stripped: what matters is the text a person reads, and
        # the address has to survive being copied out of a terminal in one
        # piece rather than arriving with escape sequences inside it.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", seen)
        assert "CACOPHONY_HANDSHAKE" in plain
        assert "not the window itself" in plain
        assert "?token=" in plain

    def test_it_points_at_a_shell_that_exists_when_one_does(self) -> None:
        from cacophony.cli.main import _shell_binary

        named = _shell_binary()
        assert "cacophony-desktop" in named or "build.sh" in named


class TestTheShellIsConfigured:
    """What can be checked without a Rust toolchain.

    The shell is built and run in CI; these assert the things that would
    silently rot — a handshake prefix that drifted from the backend's, a build
    that points at a Studio directory nobody produces.
    """

    def test_the_prefix_matches_the_backend(self) -> None:
        """Two constants, two languages, one wire format."""
        from cacophony.desktop.sidecar import HANDSHAKE_PREFIX

        source = (DESKTOP / "src" / "main.rs").read_text(encoding="utf-8")
        assert f'HANDSHAKE_PREFIX: &str = "{HANDSHAKE_PREFIX}"' in source

    def test_the_version_matches_the_backend(self) -> None:
        source = (DESKTOP / "src" / "main.rs").read_text(encoding="utf-8")
        assert f"HANDSHAKE_VERSION: u64 = {HANDSHAKE_VERSION}" in source

    def test_it_serves_the_studio_the_build_produces(self) -> None:
        config = json.loads((DESKTOP / "tauri.conf.json").read_text(encoding="utf-8"))
        target = (DESKTOP / config["build"]["frontendDist"]).resolve()
        assert target == (REPO / "backend" / "cacophony" / "api" / "static").resolve()

    def test_the_data_url_feature_is_enabled(self) -> None:
        """The failure window renders from a data URL.

        Without the feature Tauri refuses the URL and the shell panics — so the
        one path whose entire job is to explain a failure became the loudest
        failure. Found by running the binary with no backend on PATH.
        """
        cargo = (DESKTOP / "Cargo.toml").read_text(encoding="utf-8")
        assert "webview-data-url" in cargo

    def test_the_backend_is_overridable(self) -> None:
        """So a checkout can point at its virtualenv without installing."""
        source = (DESKTOP / "src" / "main.rs").read_text(encoding="utf-8")
        assert "CACOPHONY_BACKEND" in source


class TestServeIsUnchanged:
    """Section 41's constraint, checked on the command rather than the app."""

    def test_serve_still_takes_a_fixed_port_and_no_token(self) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        result = CliRunner().invoke(app, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--port" in result.stdout
        assert "token" not in result.stdout.lower()

    def test_desktop_is_a_separate_command(self) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        result = CliRunner().invoke(app, ["desktop", "--help"])
        assert result.exit_code == 0
        assert "handshake" in result.stdout.lower()

    def test_both_build_the_same_application(self) -> None:
        """No desktop mode in the backend: one `create_app`, two callers."""
        cli = (REPO / "backend" / "cacophony" / "cli" / "main.py").read_text(encoding="utf-8")
        sidecar = (REPO / "backend" / "cacophony" / "desktop" / "sidecar.py").read_text(
            encoding="utf-8"
        )
        assert "from ..api.app import create_app" in cli
        assert "from ..api.app import create_app" in sidecar
