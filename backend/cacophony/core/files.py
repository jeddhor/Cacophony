"""Writing files without losing the old one (design document sections 32, 74).

A schema is the single source of truth for a dataset, so the moment between
truncating the file and finishing the write is a moment in which a crash costs
the user their project. ``cacophony transform --in-place`` already knew this and
wrote beside its target before swapping; the Studio's schema editor did not, and
this module is what makes the two agree.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .errors import OutputError

__all__ = ["atomic_write_text"]


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Replace ``path`` with ``text``, or leave it exactly as it was.

    Written to a temporary file in the same directory - the same directory
    because ``os.replace`` is only atomic within one filesystem - flushed to
    disk, and renamed over the target. A reader either sees the old file or the
    new one, never a half-written one, and a crash costs the write rather than
    the file.
    """
    target = Path(path)
    temporary = target.with_name(f".{target.name}.cacophony-tmp")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            # The rename is atomic; the contents reaching the disk is not, so a
            # power failure could otherwise leave an empty file under the right
            # name, which is worse than the failure it was meant to prevent.
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        # Suppressed: if the directory itself is the problem, removing a file
        # inside it fails too, and the error worth reporting is the first one.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise OutputError(f"could not write {target}: {exc}") from exc
    return target
