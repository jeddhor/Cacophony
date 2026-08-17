"""Running Cacophony as a desktop application (design document section 41).

    Cacophony should eventually feel like a desktop application while retaining
    web architecture. Tauri is preferable if practical because the application
    primarily needs to host the web UI while the Python backend performs
    generation. However, web deployment should remain possible.

That last sentence is the constraint the whole design obeys. There is no desktop
*mode* in the backend: the desktop application starts the same server
``cacophony serve`` starts, serving the same Studio over the same API. The shell
around it happens to be a native window instead of a browser tab.

So this package is not an alternative architecture. It is a **handshake**:

    $ cacophony desktop --handshake
    {"url": "http://127.0.0.1:41287", "token": "…", "pid": 8123}
    …server runs until stdin closes or SIGTERM arrives…

The shell spawns that, reads one line of JSON, and points a window at the URL.
Everything hard about it is in the four properties below, and each is a way a
desktop app goes wrong that a served one does not.

**A free port, chosen by the operating system.** A fixed 8765 collides with the
copy of ``cacophony serve`` somebody already has running, and a desktop
application that refuses to start because a terminal is busy is a bad desktop
application. The port is bound before the handshake is printed, so the shell
never races a server that is not listening yet.

**Loopback, with a token.** A local HTTP server is reachable by every other
process on the machine, including other users on a shared box. A browser tab is
an explicit act; a desktop window is not, and one that quietly opened an
unauthenticated generation API would be a surprise. The token is generated per
launch, passed to the window, and required by the API.

**The server dies with the window.** A generator left running after its window
closed is the classic desktop-app failure: invisible, still writing files, still
holding a model server's attention. The sidecar therefore watches its own stdin -
when the shell exits, for any reason including a crash, the pipe closes and the
server stops. That is more reliable than a signal handler, because it survives
the shell being killed rather than asked.

**Nothing about it mentions Python.** The window opens on the Studio; the
interpreter, the port and the token are implementation details of a program the
user double-clicked.
"""

from __future__ import annotations

from .sidecar import (
    HANDSHAKE_VERSION,
    Handshake,
    free_port,
    make_token,
    run_sidecar,
    wait_until_ready,
)

__all__ = [
    "HANDSHAKE_VERSION",
    "Handshake",
    "free_port",
    "make_token",
    "run_sidecar",
    "wait_until_ready",
]
