"""One place that decides whether a path may be opened (design document §36).

`cacophony serve` on a port other machines can reach confines every path a
*request* names to `--allow-root`. That was one check per route, which is a
design that works exactly until something else opens a file - and a schema is
something else: `generator: lookup, path: /etc/passwd` is a file read that
arrives as data rather than as a parameter, reaches the generator through the
compiler, and never passes a route at all.

So the policy lives here, and the generator's own `resolve_path` consults it.
Anything a schema names goes through that method; anything a request names goes
through `RunService.permitted`. Both end up in `check` below.

Unset means unrestricted, which is the right default: the command line can read
whatever the shell that started it can read, and pretending otherwise would be
theatre. The API sets it for the duration of anything it does on behalf of a
caller - including the background task a run executes in, which inherits the
context it was created in.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import PathNotAllowedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Sequence

__all__ = ["PathPolicy", "active_policy", "confined_to", "readable"]


class PathPolicy:
    """The directories a caller may read from and write to."""

    __slots__ = ("roots",)

    def __init__(self, roots: Sequence[str | Path]) -> None:
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)

    def check(self, path: str | Path, *, what: str = "path") -> Path:
        """Resolve ``path``, refusing anything outside the roots.

        Resolved before it is checked, so ``..`` and a symlink both land where
        they really point rather than where they claim to.
        """
        resolved = Path(path).expanduser().resolve()
        for root in self.roots:
            if resolved == root or root in resolved.parents:
                return resolved
        roots = ", ".join(str(root) for root in self.roots)
        raise PathNotAllowedError(
            f"{what} {resolved} is outside the directories this server may use ({roots})."
        )


_POLICY: ContextVar[PathPolicy | None] = ContextVar("cacophony_path_policy", default=None)


def active_policy() -> PathPolicy | None:
    """The policy in force here, if any."""
    return _POLICY.get()


@contextlib.contextmanager
def confined_to(roots: Sequence[str | Path] | None) -> Iterator[PathPolicy | None]:
    """Apply a policy for the duration of a block.

    ``None`` clears it, so an unconfined service is explicit about being
    unconfined rather than inheriting whatever ran before it.
    """
    policy = PathPolicy(roots) if roots is not None else None
    token = _POLICY.set(policy)
    try:
        yield policy
    finally:
        _POLICY.reset(token)


def readable(path: str | Path, *, what: str = "file") -> Path:
    """The path, if the policy in force allows reading it."""
    policy = _POLICY.get()
    if policy is None:
        return Path(path)
    return policy.check(path, what=what)
